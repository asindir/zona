
from __future__ import annotations

import csv
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from config import (
    CATEGORY, SYMBOL, LONG_IDX, SHORT_IDX,
    RECONCILE_ATTEMPTS, MAX_UNKNOWN_POLLS,
)
from state import state, save_state

log = logging.getLogger("ada_controller")
D = lambda x: Decimal(str(x or "0"))

BASE_DIR = Path(__file__).resolve().parent
CSV_FILE = BASE_DIR / "ADAUSDT_5m.csv"

# Уточнить по instrument info Bybit перед окончательным тестом.
PRICE_TICK = Decimal("0.0001")
QTY_STEP = Decimal("1")
MIN_QTY = Decimal("1")

# Комиссия задаётся отдельно. Пока нулевая, чтобы не выдумывать тариф.
FEE_RATE = Decimal("0")


@dataclass
class Candle:
    timestamp: int
    datetime_utc: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal


class HistoricalExchange:
    def __init__(self, filename=CSV_FILE):
        self.filename = Path(filename)
        self.candles = self._load_csv()
        self.index = -1
        self.current = None

        self.orders = {}
        self.executions = []
        self.positions = {
            LONG_IDX: {
                "positionIdx": LONG_IDX,
                "side": "Buy",
                "size": Decimal("0"),
                "avgPrice": Decimal("0"),
            },
            SHORT_IDX: {
                "positionIdx": SHORT_IDX,
                "side": "Sell",
                "size": Decimal("0"),
                "avgPrice": Decimal("0"),
            },
        }

        self.ambiguities = []
        self.cycle_id = None

    def _load_csv(self):
        if not self.filename.exists():
            raise FileNotFoundError(self.filename)

        result = []
        with self.filename.open(
            "r", encoding="utf-8-sig", newline=""
        ) as f:
            reader = csv.DictReader(f)

            required = {
                "timestamp", "datetime_utc",
                "open", "high", "low", "close",
            }
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(
                    f"В CSV отсутствуют колонки: "
                    f"{required - set(reader.fieldnames or [])}"
                )

            for row in reader:
                result.append(Candle(
                    timestamp=int(row["timestamp"]),
                    datetime_utc=row["datetime_utc"],
                    open=D(row["open"]),
                    high=D(row["high"]),
                    low=D(row["low"]),
                    close=D(row["close"]),
                    volume=D(row.get("volume")),
                    turnover=D(row.get("turnover")),
                ))

        result.sort(key=lambda x: x.timestamp)

        for a, b in zip(result, result[1:]):
            if b.timestamp <= a.timestamp:
                raise ValueError("Повторяющиеся или неверные timestamp")

            if b.timestamp - a.timestamp != 300_000:
                log.warning(
                    "Разрыв истории: %s -> %s",
                    a.datetime_utc, b.datetime_utc,
                )

        if not result:
            raise ValueError("CSV пустой")

        return result

    # --------------------------------------------------
    # Управление историческим временем
    # --------------------------------------------------

    def advance_candle(self):
        """Обработать следующую свечу."""
        if self.index + 1 >= len(self.candles):
            return None

        self.index += 1
        self.current = self.candles[self.index]

        self._process_resting_orders(self.current)
        return self.current

    def advance_until(self, predicate, max_steps=None):
        """
        Продвигать историю, пока predicate не станет True.
        Используется неблокирующими ожиданиями контроллера.
        """
        steps = 0
        limit = max_steps or len(self.candles)

        while not predicate() and steps < limit:
            if self.advance_candle() is None:
                break
            steps += 1

        return predicate()

    def finished(self):
        return self.index >= len(self.candles) - 1

    def price(self):
        if self.current is None:
            return self.candles[0].open
        return self.current.close

    def timestamp(self):
        if self.current is None:
            return self.candles[0].timestamp
        return self.current.timestamp

    # --------------------------------------------------
    # Ордера
    # --------------------------------------------------

    def place(self, request):
        side = request["side"]
        order_type = request["orderType"]
        qty = D(request["qty"])
        idx = int(request["positionIdx"])

        if side not in ("Buy", "Sell"):
            raise ValueError(f"Неверная сторона: {side}")

        if idx not in (LONG_IDX, SHORT_IDX):
            raise ValueError(f"Неверный positionIdx: {idx}")

        if qty <= 0 or qty % QTY_STEP != 0:
            raise ValueError(f"Некорректное количество: {qty}")

        link_id = request.get("orderLinkId")

        # Идемпотентность по orderLinkId.
        for existing in self.orders.values():
            if link_id and existing["orderLinkId"] == link_id:
                return self.order_result(existing)

        order_id = str(uuid.uuid4())

        order = {
            "orderId": order_id,
            "orderLinkId": link_id or order_id,
            "symbol": SYMBOL,
            "side": side,
            "orderType": order_type,
            "qty": qty,
            "price": D(request.get("price")),
            "triggerPrice": D(request.get("triggerPrice")),
            "triggerDirection": request.get("triggerDirection"),
            "positionIdx": idx,
            "reduceOnly": bool(request.get("reduceOnly", False)),
            "orderStatus": "New",
            "cumExecQty": Decimal("0"),
            "avgPrice": Decimal("0"),
            "createdTime": self.timestamp(),
        }

        self.orders[order_id] = order

        # Рыночный ордер исполняется по open текущей свечи.
        if order_type == "Market":
            if self.current is None:
                order["orderStatus"] = "Rejected"
                raise RuntimeError(
                    "Сначала вызови advance_candle()"
                )
            self._fill(order, self.current.open)

        return self.order_result(order)

    def cancel(self, order_id=None, order_link_id=None):
        order = self._find(order_id, order_link_id)

        if order is None:
            return self.response({}, code=110001, msg="Order not found")

        if order["orderStatus"] in ("New", "Untriggered"):
            order["orderStatus"] = "Cancelled"

        return self.order_result(order)

    def _find(self, order_id=None, order_link_id=None):
        for order in self.orders.values():
            if order_id and order["orderId"] == order_id:
                return order
            if order_link_id and order["orderLinkId"] == order_link_id:
                return order
        return None

    def _process_resting_orders(self, candle):
        active = [
            o for o in self.orders.values()
            if o["orderStatus"] in ("New", "Untriggered")
        ]

        touched = []

        for order in active:
            if self._is_touched(order, candle):
                touched.append(order)

        # OHLC не показывает последовательность движения цены.
        # При нескольких затронутых заявках не придумываем порядок.
        if len(touched) > 1:
            self.ambiguities.append({
                "timestamp": candle.timestamp,
                "datetime_utc": candle.datetime_utc,
                "order_ids": [o["orderId"] for o in touched],
                "reason": "Неизвестен внутрисвечной порядок касаний",
            })
            return

        for order in touched:
            if order["orderType"] == "Limit":
                self._fill(order, order["price"])
            elif order.get("triggerPrice", 0) > 0:
                self._fill(order, order["triggerPrice"])

    def _is_touched(self, order, candle):
        trigger = order["triggerPrice"]
        price = order["price"]

        if trigger > 0:
            direction = order["triggerDirection"]

            if direction == 1:
                return candle.high >= trigger
            if direction == 2:
                return candle.low <= trigger
            return False

        if order["orderType"] == "Limit":
            if order["side"] == "Buy":
                return candle.low <= price
            return candle.high >= price

        return False

    # --------------------------------------------------
    # Исполнение и позиции Hedge Mode
    # --------------------------------------------------

    def _fill(self, order, price):
        if order["orderStatus"] != "New":
            return

        idx = order["positionIdx"]
        position = self.positions[idx]
        qty = order["qty"]

        opening_side = "Buy" if idx == LONG_IDX else "Sell"
        closing_side = "Sell" if idx == LONG_IDX else "Buy"

        if order["reduceOnly"]:
            if order["side"] != closing_side:
                order["orderStatus"] = "Rejected"
                return

            qty = min(qty, position["size"])

        elif order["side"] not in (opening_side, closing_side):
            order["orderStatus"] = "Rejected"
            return

        if qty <= 0:
            order["orderStatus"] = "Cancelled"
            return

        if order["side"] == opening_side:
            old_qty = position["size"]
            new_qty = old_qty + qty

            position["avgPrice"] = (
                (position["avgPrice"] * old_qty + price * qty) / new_qty
            )
            position["size"] = new_qty

        else:
            if qty > position["size"]:
                qty = position["size"]

            position["size"] -= qty

            if position["size"] == 0:
                position["avgPrice"] = Decimal("0")

        fee = qty * price * FEE_RATE

        execution = {
            "execId": str(uuid.uuid4()),
            "orderId": order["orderId"],
            "orderLinkId": order["orderLinkId"],
            "execTime": self.timestamp(),
            "side": order["side"],
            "positionIdx": idx,
            "execQty": str(qty),
            "execPrice": str(price),
            "execFee": str(fee),
            "execType": "Trade",
        }

        self.executions.append(execution)

        order["cumExecQty"] = qty
        order["avgPrice"] = price
        order["orderStatus"] = "Filled"

    # --------------------------------------------------
    # Формат ответов, совместимый с Bybit V5
    # --------------------------------------------------

    @staticmethod
    def response(result, code=0, msg="OK"):
        return {
            "retCode": code,
            "retMsg": msg,
            "result": result,
        }

    def order_result(self, order):
        return self.response({
            "orderId": order["orderId"],
            "orderLinkId": order["orderLinkId"],
            "orderStatus": order["orderStatus"],
            "symbol": order["symbol"],
            "side": order["side"],
            "qty": str(order["qty"]),
            "price": str(order["price"]),
            "triggerPrice": str(order["triggerPrice"]),
            "positionIdx": order["positionIdx"],
            "reduceOnly": order["reduceOnly"],
            "cumExecQty": str(order["cumExecQty"]),
            "avgPrice": str(order["avgPrice"]),
        })

    def positions_result(self):
        rows = []

        for p in self.positions.values():
            if p["size"] == 0:
                continue

            rows.append({
                "symbol": SYMBOL,
                "positionIdx": p["positionIdx"],
                "side": p["side"],
                "size": str(p["size"]),
                "avgPrice": str(p["avgPrice"]),
            })

        return self.response({"list": rows, "nextPageCursor": ""})

    def orders_result(self, include_history=False):
        statuses = (
            None if include_history else ("New", "Untriggered")
        )

        rows = []
        for order in self.orders.values():
            if statuses is None or order["orderStatus"] in statuses:
                rows.append({
                    "orderId": order["orderId"],
                    "orderLinkId": order["orderLinkId"],
                    "symbol": SYMBOL,
                    "side": order["side"],
                    "orderStatus": order["orderStatus"],
                    "qty": str(order["qty"]),
                    "price": str(order["price"]),
                    "positionIdx": order["positionIdx"],
                    "reduceOnly": order["reduceOnly"],
                    "cumExecQty": str(order["cumExecQty"]),
                    "avgPrice": str(order["avgPrice"]),
                })

        return self.response({"list": rows, "nextPageCursor": ""})


EX = HistoricalExchange()


# ======================================================
# Публичный интерфейс. Имена совпадают с exchange.py
# ======================================================

def D_value(value: Any) -> Decimal:
    return D(value)


def check(response: dict, operation="Test exchange") -> dict:
    if not isinstance(response, dict):
        raise RuntimeError(f"{operation}: некорректный ответ")
    if response.get("retCode") != 0:
        raise RuntimeError(
            f"{operation}: ErrCode {response.get('retCode')}: "
            f"{response.get('retMsg')}"
        )
    return response.get("result", {})


def round_price(price, direction="nearest"):
    value = D(price) / PRICE_TICK
    rounding = ROUND_DOWN
    if direction == "up":
        rounding = "ROUND_UP"
    return value.to_integral_value(rounding=rounding) * PRICE_TICK


def fmt(value):
    return format(D(value).normalize(), "f")


def validate_qty(qty):
    value = D(qty)
    value = (value / QTY_STEP).to_integral_value(
        rounding=ROUND_DOWN
    ) * QTY_STEP

    if value < MIN_QTY:
        raise ValueError(f"Количество меньше минимума: {value}")

    return value


def get_positions():
    return check(EX.positions_result()).get("list", [])


def get_position(position_idx):
    for p in get_positions():
        if int(p["positionIdx"]) == int(position_idx):
            return p
    return None


def get_position_qty(position_idx):
    p = get_position(position_idx)
    return D(p["size"]) if p else Decimal("0")


def get_open_orders():
    return check(EX.orders_result()).get("list", [])


def _matches(row, item):
    if item.get("orderId") and row.get("orderId") == item["orderId"]:
        return True
    if (
        item.get("orderLinkId")
        and row.get("orderLinkId") == item["orderLinkId"]
    ):
        return True
    return False


def find_order(name, item):
    for row in check(EX.orders_result(include_history=True)).get("list", []):
        if _matches(row, item):
            return row
    return None


def get_order_executions(
    order_id=None,
    start_time=None,
    end_time=None,
):
    rows = EX.executions

    if order_id:
        rows = [r for r in rows if r["orderId"] == str(order_id)]

    if start_time is not None:
        rows = [r for r in rows if int(r["execTime"]) >= int(start_time)]

    if end_time is not None:
        rows = [r for r in rows if int(r["execTime"]) <= int(end_time)]

    return [dict(r) for r in rows]


def summarize_executions(executions):
    qty = Decimal("0")
    value = Decimal("0")
    fee = Decimal("0")
    first_time = None
    last_time = None
    details = []

    for e in executions:
        q = D(e.get("execQty"))
        p = D(e.get("execPrice"))
        f = D(e.get("execFee"))

        if q <= 0 or p <= 0:
            continue

        qty += q
        value += q * p
        fee += f

        t = e.get("execTime")
        if t:
            t = int(t)
            first_time = t if first_time is None else min(first_time, t)
            last_time = t if last_time is None else max(last_time, t)

        details.append(dict(e))

    return {
        "qty": qty,
        "value": value,
        "average_price": value / qty if qty else Decimal("0"),
        "fee": fee,
        "first_exec_time": first_time,
        "last_exec_time": last_time,
        "executions": details,
    }


def get_execution_status(name, item):
    order = find_order(name, item)
    order_id = (order or {}).get("orderId") or item.get("orderId")

    executions = (
        get_order_executions(str(order_id)) if order_id else []
    )
    summary = summarize_executions(executions)

    return {
        "name": name,
        "order": order,
        "orderStatus": (order or {}).get("orderStatus"),
        "cumExecQty": summary["qty"],
        "avgPrice": summary["average_price"],
        "execValue": summary["value"],
        "execFee": summary["fee"],
        "firstExecTime": summary["first_exec_time"],
        "lastExecTime": summary["last_exec_time"],
        "executions": summary["executions"],
        "verified": bool(order or executions),
    }


def reconcile_order(name, item):
    return get_execution_status(name, item)


def order_status(name, item):
    return reconcile_order(name, item).get("orderStatus")


def is_filled(status):
    return status == "Filled"


def is_partial(status):
    return status == "PartiallyFilled"


def is_active(status):
    return status in {
        "New", "Created", "Untriggered",
        "Triggered", "PartiallyFilled",
    }


def _link_id(name):
    return f"TEST-{name[:8]}-{uuid.uuid4().hex[:12]}"[:36]


def place_once(
    name,
    side,
    order_type,
    qty,
    position_idx,
    price=None,
    trigger_price=None,
    reduce_only=False,
    trigger_direction=None,
):
    orders = state.setdefault("orders", {})
    existing = orders.get(name)

    if existing and existing.get("orderId"):
        return existing

    if existing and existing.get("submit_status") in {
        "SUBMITTING", "SUBMIT_UNKNOWN"
    }:
        raise RuntimeError(
            f"{name}: неизвестен результат предыдущей отправки"
        )

    quantity = validate_qty(qty)
    link_id = (
        existing.get("orderLinkId") if existing
        else _link_id(name)
    )

    request = {
        "side": side,
        "orderType": order_type,
        "qty": fmt(quantity),
        "positionIdx": int(position_idx),
        "orderLinkId": link_id,
        "reduceOnly": bool(reduce_only),
    }

    if price is not None:
        if order_type != "Limit":
            raise ValueError("price разрешён только для Limit")
        request["price"] = fmt(round_price(price))

    if trigger_price is not None:
        if trigger_direction not in (1, 2):
            raise ValueError("Неверный triggerDirection")
        request["triggerPrice"] = fmt(round_price(trigger_price))
        request["triggerDirection"] = int(trigger_direction)

    elif trigger_direction is not None:
        raise ValueError("triggerDirection без triggerPrice")

    result = check(EX.place(request), f"place_order {name}")

    item = {
        **request,
        "name": name,
        "orderId": result.get("orderId"),
        "orderLinkId": result.get("orderLinkId") or link_id,
        "orderStatus": result.get("orderStatus"),
        "submit_status": "SUBMITTED",
        "requested_qty": fmt(quantity),
        "created_at": EX.timestamp(),
    }

    orders[name] = item
    save_state()
    return item


def cancel_order(name, item):
    result = check(
        EX.cancel(
            order_id=item.get("orderId"),
            order_link_id=item.get("orderLinkId"),
        ),
        f"cancel_order {name}",
    )

    item["cancel_verified"] = True
    item["orderStatus"] = result.get("orderStatus")
    save_state()
    return True


def cancel_all_strategy_orders():
    for order in get_open_orders():
        check(
            EX.cancel(order_id=order["orderId"]),
            "cancel_all_strategy_orders",
        )
    return not get_open_orders()


def wait_filled(name, item):
    """
    Неблокирующее ожидание: если ордер ещё не исполнен,
    контроллер должен продолжить цикл опроса.
    """
    return reconcile_order(name, item)


def wait_position(position_idx, expected_qty, timeout=60):
    expected = D(expected_qty)
    actual = get_position_qty(position_idx)

    if actual == expected:
        return get_position(position_idx) or {
            "positionIdx": position_idx,
            "size": "0",
        }

    raise RuntimeError(
        f"Историческое время ещё не продвинуто: "
        f"positionIdx={position_idx}, expected={expected}, actual={actual}"
    )


def verify_flat():
    return (
        get_position_qty(LONG_IDX) == 0
        and get_position_qty(SHORT_IDX) == 0
        and len(get_open_orders()) == 0
    )


def close_position_market(position_idx, side, qty=None):
    actual = get_position_qty(position_idx)

    if actual == 0:
        return None

    close_qty = validate_qty(qty if qty is not None else actual)

    if close_qty > actual:
        raise RuntimeError("Закрытие превышает размер позиции")

    return place_once(
        name=f"CLOSE{position_idx}_{uuid.uuid4().hex[:6]}",
        side=side,
        order_type="Market",
        qty=close_qty,
        position_idx=position_idx,
        reduce_only=True,
    )


def validate_account():
    # В эмуляторе API-проверки инструмента нет.
    if not EX.candles:
        raise RuntimeError("Исторические свечи не загружены")
    get_positions()
    get_open_orders()
    log.info(
        "TestExchange готов: %s свечей, %s — %s",
        len(EX.candles),
        EX.candles[0].datetime_utc,
        EX.candles[-1].datetime_utc,
    )
    return True


# ------------------------------------------------------
# Дополнительные функции для тестового запуска
# ------------------------------------------------------

def advance_candle():
    """Продвинуть эмулятор на одну историческую свечу."""
    candle = EX.advance_candle()

    if candle is None:
        log.info("Исторические данные закончились")
        return None

    log.info(
        "TEST CANDLE %s O=%s H=%s L=%s C=%s",
        candle.datetime_utc,
        candle.open,
        candle.high,
        candle.low,
        candle.close,
    )
    return candle


def history_finished():
    return EX.finished()


def get_ambiguities():
    return list(EX.ambiguities)


def get_current_candle():
    return EX.current


def get_current_timestamp():
    return EX.timestamp()
