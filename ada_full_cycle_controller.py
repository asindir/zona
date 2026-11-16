
import os
import json
import time
import uuid
import logging
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from dotenv import load_dotenv
from pybit.unified_trading import HTTP


# ============================================================
# ADA FULL CYCLE CONTROLLER — BYBIT DEMO
# Восстановление ордеров, подробное логирование, без дублей
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "api.env")

STATE_FILE = BASE_DIR / "ada_full_cycle_state.json"
LOCK_FILE = BASE_DIR / "ada_full_cycle.lock"
LOG_FILE = BASE_DIR / "ada_full_cycle.log"

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

CATEGORY = "linear"
SYMBOL = "ADAUSDT"

# ПАРАМЕТРЫ СТРАТЕГИИ — НЕ ИЗМЕНЯТЬ
ZONE_PERCENT = Decimal("0.7")
TP1_PERCENT = Decimal("0.7")
TP2_PERCENT = Decimal("1.9")
TP3_PERCENT = Decimal("0.3")
STOP_BUFFER_PERCENT = Decimal("-0.06")

LOT_BUY1 = Decimal("100")
LOT_SELL2 = Decimal("160")
LOT_BUY3 = Decimal("1200")

LONG_IDX = 1
SHORT_IDX = 2

POLL_SECONDS = 2
RECONCILE_ATTEMPTS = 5
RECONCILE_DELAY = 1
MAX_UNKNOWN_POLLS = 30

EXPECTED_QTY = {
    "B1": Decimal("100"),
    "TP1": Decimal("100"),
    "S2": Decimal("160"),
    "TP2": Decimal("160"),
    "B3": Decimal("1200"),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

log = logging.getLogger("ADA_CONTROLLER")


# ============================================================
# DEMO CONNECTION
# ============================================================

if not API_KEY or not API_SECRET:
    raise SystemExit(
        "ОШИБКА: BYBIT_API_KEY или BYBIT_API_SECRET отсутствует в api.env"
    )

session = HTTP(
    testnet=False,
    demo=True,
    api_key=API_KEY,
    api_secret=API_SECRET,
    recv_window=20000
)


def check(response, name="API"):
    if not isinstance(response, dict):
        raise RuntimeError(f"{name}: некорректный ответ API")

    if response.get("retCode") != 0:
        raise RuntimeError(
            f"{name}: retCode={response.get('retCode')} "
            f"retMsg={response.get('retMsg')}"
        )

    return response.get("result", {})


# ============================================================
# STATE
# ============================================================

def new_state():
    return {
        "status": "NEW",
        "orders": {},
        "prices": {},
        "created_at": time.time()
    }


def load_state():
    if not STATE_FILE.exists():
        return new_state()

    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))

    data.setdefault("orders", {})
    data.setdefault("prices", {})

    return data


state = load_state()


def save_state():
    temp_file = STATE_FILE.with_suffix(".tmp")

    temp_file.write_text(
        json.dumps(state, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    os.replace(temp_file, STATE_FILE)


def set_status(status):
    state["status"] = status
    save_state()
    log.info("СОСТОЯНИЕ: %s", status)


# ============================================================
# LOCK
# ============================================================

def acquire_lock():
    try:
        fd = os.open(
            str(LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )

        os.write(fd, str(os.getpid()).encode())
        os.close(fd)

    except FileExistsError:
        raise RuntimeError(
            "Файл блокировки уже существует. "
            "Проверь, не работает ли другой контроллер. "
            "Не удаляй lock-файл, пока не убедишься, "
            "что другой процесс завершён."
        )


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception as error:
        log.warning("Не удалось удалить lock-файл: %s", error)


# ============================================================
# INSTRUMENT FILTERS
# ============================================================

def get_filters():
    result = check(
        session.get_instruments_info(
            category=CATEGORY,
            symbol=SYMBOL
        ),
        "Instrument info"
    )

    items = result.get("list", [])

    if not items:
        raise RuntimeError("ADAUSDT не найден.")

    item = items[0]

    return {
        "tick": Decimal(item["priceFilter"]["tickSize"]),
        "step": Decimal(item["lotSizeFilter"]["qtyStep"]),
        "min_qty": Decimal(item["lotSizeFilter"]["minOrderQty"]),
        "min_value": Decimal(
            item["lotSizeFilter"].get("minNotionalValue", "5")
        )
    }


FILTERS = get_filters()


def round_price(value, rounding=ROUND_DOWN):
    value = Decimal(str(value))
    tick = FILTERS["tick"]

    return (
        value / tick
    ).to_integral_value(rounding=rounding) * tick


def fmt(value):
    return format(Decimal(str(value)).normalize(), "f")


def validate_qty(qty, price=None):
    qty = Decimal(str(qty))
    step = FILTERS["step"]

    if qty < FILTERS["min_qty"]:
        raise RuntimeError("Количество меньше минимального.")

    if qty % step != 0:
        raise RuntimeError("Количество не соответствует qtyStep.")

    if price is not None:
        if qty * Decimal(str(price)) < FILTERS["min_value"]:
            raise RuntimeError("Сумма ордера меньше minNotionalValue.")

    return fmt(qty)


# ============================================================
# POSITIONS
# ============================================================

def get_positions():
    result = check(
        session.get_positions(
            category=CATEGORY,
            symbol=SYMBOL
        ),
        "Position info"
    )

    positions = {
        LONG_IDX: {
            "size": Decimal("0"),
            "avg": Decimal("0")
        },
        SHORT_IDX: {
            "size": Decimal("0"),
            "avg": Decimal("0")
        }
    }

    for p in result.get("list", []):
        idx = int(p.get("positionIdx", 0))

        if idx in positions:
            positions[idx] = {
                "size": Decimal(p.get("size") or "0"),
                "avg": Decimal(p.get("avgPrice") or "0")
            }

    return positions


# ============================================================
# OPEN ORDERS
# ============================================================

def get_open_orders():
    result = check(
        session.get_open_orders(
            category=CATEGORY,
            symbol=SYMBOL,
            openOnly=0,
            limit=50
        ),
        "Open orders"
    )

    return result.get("list", [])


# ============================================================
# ORDER LOOKUP
# ============================================================

def find_order(link_id=None, order_id=None):
    if not link_id and not order_id:
        return None

    # 1. Открытые ордера
    for order in get_open_orders():
        if (
            (link_id and order.get("orderLinkId") == link_id)
            or
            (order_id and order.get("orderId") == order_id)
        ):
            return order

    # 2. История по orderId
    if order_id:
        result = check(
            session.get_order_history(
                category=CATEGORY,
                symbol=SYMBOL,
                orderId=order_id,
                limit=10
            ),
            "Order history by ID"
        )

        for order in result.get("list", []):
            if order.get("orderId") == order_id:
                return order

    # 3. История по linkId
    if link_id:
        result = check(
            session.get_order_history(
                category=CATEGORY,
                symbol=SYMBOL,
                orderLinkId=link_id,
                limit=10
            ),
            "Order history by link"
        )

        for order in result.get("list", []):
            if order.get("orderLinkId") == link_id:
                return order

    return None


# ============================================================
# EXECUTION HISTORY
# ============================================================

def get_execution_status(name, item):
    order_id = item.get("order_id")

    if not order_id:
        return None

    result = check(
        session.get_executions(
            category=CATEGORY,
            symbol=SYMBOL,
            orderId=order_id,
            limit=100
        ),
        "Execution history"
    )

    executions = result.get("list", [])

    if not executions:
        return None

    total_qty = Decimal("0")
    total_value = Decimal("0")

    for execution in executions:
        qty = Decimal(execution.get("execQty") or "0")
        price = Decimal(execution.get("execPrice") or "0")

        total_qty += qty
        total_value += qty * price

    if total_qty <= 0:
        return None

    expected = Decimal(
        str(
            item.get("requested_qty")
            or EXPECTED_QTY.get(name, "0")
        )
    )

    if expected <= 0:
        return None

    avg_price = total_value / total_qty

    status = (
        "Filled"
        if total_qty >= expected
        else "PartiallyFilled"
    )

    return {
        "orderId": order_id,
        "orderLinkId": item.get("link_id"),
        "orderStatus": status,
        "cumExecQty": fmt(total_qty),
        "avgPrice": fmt(avg_price),
        "qty": fmt(expected)
    }


# ============================================================
# RECONCILIATION
# ============================================================

def reconcile_order(name, attempts=RECONCILE_ATTEMPTS):
    item = state["orders"].get(name)

    if not item:
        log.error("%s: запись отсутствует в state.json", name)
        return None

    for attempt in range(1, attempts + 1):
        try:
            found = find_order(
                link_id=item.get("link_id"),
                order_id=item.get("order_id")
            )

            if found:
                item["last_status"] = found.get("orderStatus")
                item["last_checked"] = time.time()
                save_state()

                return found

            execution = get_execution_status(name, item)

            if execution:
                item["last_status"] = execution["orderStatus"]
                item["last_checked"] = time.time()
                save_state()

                return execution

            log.info(
                "Сверка %s: запись пока не найдена (%s/%s)",
                name, attempt, attempts
            )

        except Exception as error:
            log.warning(
                "Ошибка сверки %s (%s/%s): %s",
                name, attempt, attempts, error
            )

        if attempt < attempts:
            time.sleep(RECONCILE_DELAY)

    return None


def order_status(name):
    item = state["orders"].get(name)

    if not item:
        log.error("%s: отсутствует запись в state.json", name)
        return None

    try:
        order = reconcile_order(name, attempts=1)

        if order is None:
            log.warning(
                "%s: статус не получен | orderId=%s | linkId=%s",
                name,
                item.get("order_id"),
                item.get("link_id")
            )

            return None

        log.info(
            "%s: статус=%s | исполнено=%s/%s | orderId=%s",
            name,
            order.get("orderStatus"),
            order.get("cumExecQty", "0"),
            order.get("qty", item.get("requested_qty", "?")),
            order.get("orderId")
        )

        return order

    except Exception as error:
        log.exception(
            "%s: ошибка получения статуса: %s",
            name, error
        )

        return None


def is_filled(order):
    return (
        order is not None
        and order.get("orderStatus") == "Filled"
    )


def is_partial(order):
    return (
        order is not None
        and order.get("orderStatus") in (
            "PartiallyFilled",
            "PartiallyFilledCanceled"
        )
    )


def is_active(order):
    return (
        order is not None
        and order.get("orderStatus") in (
            "New",
            "Untriggered",
            "PartiallyFilled"
        )
    )


# ============================================================
# SAFE ORDER SUBMISSION
# ============================================================

def place_once(name, params):
    old = state["orders"].get(name)

    # Если отправка уже начиналась, повтор запрещён.
    if old and old.get("submitted"):
        log.info(
            "%s: ранее уже отправлялся. Проверяю существующий ордер.",
            name
        )

        found = reconcile_order(name)

        if found:
            log.info(
                "%s уже найден: %s",
                name, found.get("orderStatus")
            )

            return found

        if old.get("order_id"):
            log.warning(
                "%s: orderId сохранён, статус неизвестен. "
                "Повторная отправка запрещена.",
                name
            )

            return {
                "orderId": old["order_id"],
                "orderLinkId": old.get("link_id"),
                "orderStatus": "Unknown"
            }

        raise RuntimeError(
            f"{name}: предыдущая отправка не подтверждена. "
            "Нужна ручная сверка."
        )

    if old:
        link_id = old["link_id"]
    else:
        link_id = "ada" + uuid.uuid4().hex

        state["orders"][name] = {
            "link_id": link_id,
            "submitted": False
        }

        save_state()

    params = dict(params)
    params["orderLinkId"] = link_id

    state["orders"][name]["submitted"] = True
    state["orders"][name]["request_started"] = time.time()
    state["orders"][name]["requested_qty"] = params.get("qty")

    save_state()

    log.info(
        "ОТПРАВКА %s | linkId=%s",
        name, link_id
    )

    try:
        response = session.place_order(**params)
        result = check(response, f"Place {name}")

        order_id = result.get("orderId")

        if not order_id:
            raise RuntimeError(
                f"{name}: Bybit не вернул orderId. "
                "Повтор запрещён."
            )

        state["orders"][name]["order_id"] = order_id
        state["orders"][name]["accepted"] = True
        save_state()

        log.info(
            "%s принят Bybit. orderId=%s",
            name, order_id
        )

    except Exception as error:
        log.error(
            "%s: ошибка отправки или ответа: %s",
            name, error
        )

        found = reconcile_order(name, attempts=15)

        if found:
            return found

        raise RuntimeError(
            f"{name}: результат отправки неизвестен. "
            "Повторная отправка запрещена. "
            "Требуется ручная сверка."
        )

    found = reconcile_order(name, attempts=5)

    if found:
        return found

    log.warning(
        "%s: успешный ответ и orderId сохранены, "
        "но история пока не показывает ордер. "
        "Повторная отправка запрещена.",
        name
    )

    return {
        "orderId": order_id,
        "orderLinkId": link_id,
        "orderStatus": "Unknown"
    }


# ============================================================
# CANCEL ORDER
# ============================================================

def cancel_order(name):
    item = state["orders"].get(name)

    if not item:
        log.warning("%s: запись для отмены отсутствует", name)
        return

    order = reconcile_order(name, attempts=5)

    if not order:
        raise RuntimeError(
            f"{name}: статус неизвестен. Отмена не отправлена."
        )

    if not is_active(order):
        log.info(
            "%s: отмена не требуется, статус=%s",
            name, order.get("orderStatus")
        )

        return

    log.warning(
        "ОТПРАВКА ОТМЕНЫ %s | orderId=%s",
        name, item.get("order_id")
    )

    check(
        session.cancel_order(
            category=CATEGORY,
            symbol=SYMBOL,
            orderLinkId=item["link_id"]
        ),
        f"Cancel {name}"
    )

    for attempt in range(1, 31):
        time.sleep(1)

        current = order_status(name)

        if current and current.get("orderStatus") in (
            "Cancelled",
            "Filled",
            "Deactivated",
            "Rejected",
            "PartiallyFilledCanceled"
        ):
            log.info(
                "%s завершён: %s",
                name, current.get("orderStatus")
            )

            return

    raise RuntimeError(
        f"Не удалось подтвердить отмену {name}"
    )


def cancel_all_strategy_orders():
    for name in list(state["orders"].keys()):
        cancel_order(name)


# ============================================================
# WAIT FOR FILL
# ============================================================

def wait_filled(name, timeout=120):
    started = time.time()
    unknown_count = 0

    log.info("ОЖИДАНИЕ ИСПОЛНЕНИЯ %s", name)

    while time.time() - started < timeout:
        order = order_status(name)

        if order is None:
            unknown_count += 1

            if unknown_count >= MAX_UNKNOWN_POLLS:
                raise RuntimeError(
                    f"{name}: статус неизвестен. "
                    "Нужна ручная сверка."
                )

            time.sleep(POLL_SECONDS)
            continue

        unknown_count = 0

        if is_filled(order):
            log.info("%s полностью исполнен", name)
            return order

        if is_partial(order):
            raise RuntimeError(
                f"{name}: частичное исполнение. "
                "Автоматический переход запрещён."
            )

        if order.get("orderStatus") in (
            "Cancelled",
            "Rejected",
            "Deactivated"
        ):
            raise RuntimeError(
                f"{name}: статус {order.get('orderStatus')}"
            )

        time.sleep(POLL_SECONDS)

    raise RuntimeError(
        f"Истекло время ожидания исполнения {name}"
    )


# ============================================================
# POSITION CHECKS
# ============================================================

def wait_position(idx, expected, timeout=60):
    expected = Decimal(str(expected))
    started = time.time()

    while time.time() - started < timeout:
        actual = get_positions()[idx]["size"]

        if actual == expected:
            return

        time.sleep(1)

    actual = get_positions()[idx]["size"]

    raise RuntimeError(
        f"Позиция idx={idx}: ожидали {expected}, получили {actual}"
    )


def verify_flat():
    positions = get_positions()

    if (
        positions[LONG_IDX]["size"] != 0
        or positions[SHORT_IDX]["size"] != 0
    ):
        raise RuntimeError(
            f"Счёт не flat: Long={positions[LONG_IDX]['size']} "
            f"Short={positions[SHORT_IDX]['size']}"
        )

    active = get_open_orders()

    if active:
        raise RuntimeError(
            f"Остались открытые ордера: {len(active)}"
        )


# ============================================================
# MARKET CLOSE
# ============================================================

def close_position_market(idx, name):
    position = get_positions()[idx]
    qty = position["size"]

    if qty <= 0:
        return

    side = "Sell" if idx == LONG_IDX else "Buy"

    params = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": side,
        "orderType": "Market",
        "qty": validate_qty(qty),
        "positionIdx": idx,
        "reduceOnly": True,
        "timeInForce": "IOC"
    }

    place_once(name, params)

    wait_filled(name, timeout=120)
    wait_position(idx, Decimal("0"), timeout=60)


# ============================================================
# ACCOUNT VALIDATION
# ============================================================

def validate_account():
    log.info("Проверка DEMO аккаунта...")

    check(
        session.get_wallet_balance(accountType="UNIFIED"),
        "Wallet"
    )

    instrument = check(
        session.get_instruments_info(
            category=CATEGORY,
            symbol=SYMBOL
        ),
        "Instrument"
    )

    if not instrument.get("list"):
        raise RuntimeError("ADAUSDT не найден.")

    if instrument["list"][0].get("status") != "Trading":
        raise RuntimeError("ADAUSDT сейчас не торгуется.")

    positions = get_positions()

    log.info(
        "Текущие позиции: Long=%s Short=%s",
        positions[LONG_IDX]["size"],
        positions[SHORT_IDX]["size"]
    )

    if state["status"] == "NEW":
        if (
            positions[LONG_IDX]["size"] != 0
            or positions[SHORT_IDX]["size"] != 0
        ):
            raise RuntimeError(
                "Новый цикл запрещён: есть открытая позиция."
            )

        if get_open_orders():
            raise RuntimeError(
                "Новый цикл запрещён: есть открытые ордера ADAUSDT."
            )


# ============================================================
# RECOVERY FROM EMPTY ERROR
# ============================================================

def recover_empty_error_state():
    if state.get("status") != "ERROR":
        return

    if state.get("orders"):
        raise RuntimeError(
            "ERROR содержит записи ордеров. "
            "Автоматический сброс запрещён."
        )

    positions = get_positions()

    if (
        positions[LONG_IDX]["size"] != 0
        or positions[SHORT_IDX]["size"] != 0
    ):
        raise RuntimeError(
            "Нельзя сбросить ERROR: есть открытая позиция."
        )

    if get_open_orders():
        raise RuntimeError(
            "Нельзя сбросить ERROR: есть открытые ордера."
        )

    log.warning("Пустое ERROR. Возвращаю состояние NEW.")

    state.clear()
    state.update(new_state())
    save_state()


# ============================================================
# STAGE 1 — B1
# ============================================================

def stage1_b1():
    if state["status"] == "NEW":
        log.info("ЭТАП 1: B1 MARKET BUY 100 ADA")

        params = {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "side": "Buy",
            "orderType": "Market",
            "qty": validate_qty(LOT_BUY1),
            "positionIdx": LONG_IDX,
            "timeInForce": "IOC"
        }

        place_once("B1", params)
        set_status("B1_SUBMITTED")

    if state["status"] == "B1_SUBMITTED":
        order = wait_filled("B1")

        executed_qty = Decimal(
            order.get("cumExecQty") or "0"
        )

        if executed_qty != LOT_BUY1:
            raise RuntimeError(
                f"B1 исполнен {executed_qty}, ожидали {LOT_BUY1}"
            )

        positions = get_positions()

        if positions[LONG_IDX]["size"] != LOT_BUY1:
            raise RuntimeError(
                "После B1 размер Long не равен 100 ADA."
            )

        avg = Decimal(order.get("avgPrice") or "0")

        if avg <= 0:
            avg = positions[LONG_IDX]["avg"]

        if avg <= 0:
            raise RuntimeError("Не удалось определить цену B1.")

        state["prices"]["B1"] = fmt(avg)
        set_status("B1_FILLED")


# ============================================================
# STAGE 2 — TP1 + S2
# ============================================================

def stage2_orders():
    if state["status"] != "B1_FILLED":
        return

    b1 = Decimal(state["prices"]["B1"])

    tp1 = round_price(
        b1 * (1 + TP1_PERCENT / 100),
        ROUND_UP
    )

    s2 = round_price(
        b1 * (1 - ZONE_PERCENT / 100),
        ROUND_DOWN
    )

    state["prices"]["TP1"] = fmt(tp1)
    state["prices"]["S2"] = fmt(s2)
    save_state()

    log.info(
        "ЭТАП 2: TP1=%s | S2=%s",
        tp1, s2
    )

    place_once("TP1", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": "Sell",
        "orderType": "Limit",
        "qty": validate_qty(LOT_BUY1, tp1),
        "price": fmt(tp1),
        "timeInForce": "GTC",
        "positionIdx": LONG_IDX,
        "reduceOnly": True
    })

    place_once("S2", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": "Sell",
        "orderType": "Limit",
        "qty": validate_qty(LOT_SELL2, s2),
        "price": fmt(s2),
        "triggerPrice": fmt(s2),
        "triggerDirection": 2,
        "triggerBy": "LastPrice",
        "timeInForce": "GTC",
        "positionIdx": SHORT_IDX,
        "reduceOnly": False
    })

    set_status("STAGE2_ACTIVE")


# ============================================================
# MONITOR PAIR
# ============================================================

def monitor_pair(first_name, second_name):
    first_missing = 0
    second_missing = 0
    poll_number = 0
    last_heartbeat = 0

    log.info(
        "МОНИТОРИНГ НАЧАТ: %s и %s",
        first_name, second_name
    )

    while True:
        poll_number += 1

        log.info(
            "Проверка #%s: запрашиваю статусы %s и %s",
            poll_number, first_name, second_name
        )

        first = order_status(first_name)
        second = order_status(second_name)

        first_missing = first_missing + 1 if first is None else 0
        second_missing = second_missing + 1 if second is None else 0

        if (
            first_missing >= MAX_UNKNOWN_POLLS
            or second_missing >= MAX_UNKNOWN_POLLS
        ):
            raise RuntimeError(
                "Не удаётся подтвердить статусы ордеров. "
                "Мониторинг остановлен для ручной сверки."
            )

        if is_partial(first) or is_partial(second):
            raise RuntimeError(
                "Частичное исполнение одного из ордеров. "
                "Автоматический переход запрещён."
            )

        first_filled = is_filled(first)
        second_filled = is_filled(second)

        if first_filled and second_filled:
            raise RuntimeError(
                f"КОНФЛИКТ: {first_name} и {second_name} "
                "оба исполнены."
            )

        if first_filled:
            log.warning("%s ИСПОЛНЕН", first_name)
            return first_name

        if second_filled:
            log.warning("%s ИСПОЛНЕН", second_name)
            return second_name

        for name, order in (
            (first_name, first),
            (second_name, second)
        ):
            if order and order.get("orderStatus") in (
                "Cancelled",
                "Rejected",
                "Deactivated",
                "PartiallyFilledCanceled"
            ):
                raise RuntimeError(
                    f"{name}: неожиданный статус "
                    f"{order.get('orderStatus')}"
                )

        now = time.time()

        if now - last_heartbeat >= 10:
            log.info(
                "МОНИТОРИНГ РАБОТАЕТ | "
                "%s неизвестных проверок=%s | "
                "%s неизвестных проверок=%s",
                first_name,
                first_missing,
                second_name,
                second_missing
            )

            last_heartbeat = now

        time.sleep(POLL_SECONDS)


# ============================================================
# STAGE 3 — OCO MONITOR
# ============================================================

def stage3_monitor():
    log.info(
        "ЭТАП 3: контроль существующих TP1 и S2. "
        "Новые ордера не отправляются."
    )

    while state["status"] == "STAGE2_ACTIVE":
        winner = monitor_pair("TP1", "S2")

        if winner == "TP1":
            log.info("TP1 исполнен. Отменяю S2.")

            cancel_order("S2")

            positions = get_positions()

            if (
                positions[LONG_IDX]["size"] != 0
                or positions[SHORT_IDX]["size"] != 0
            ):
                raise RuntimeError(
                    "После TP1 позиции не закрыты полностью."
                )

            cancel_all_strategy_orders()
            verify_flat()
            set_status("CYCLE_COMPLETED")
            return

        if winner == "S2":
            log.info("S2 исполнен. Отменяю TP1.")

            cancel_order("TP1")

            positions = get_positions()

            if positions[LONG_IDX]["size"] != LOT_BUY1:
                raise RuntimeError(
                    "После S2 размер Long не равен 100 ADA."
                )

            if positions[SHORT_IDX]["size"] != LOT_SELL2:
                raise RuntimeError(
                    "После S2 размер Short не равен 160 ADA."
                )

            set_status("S2_FILLED")
            return


# ============================================================
# STAGE 4 — TP2 + B3
# ============================================================

def stage4_orders():
    if state["status"] != "S2_FILLED":
        return

    positions = get_positions()

    if positions[SHORT_IDX]["size"] != LOT_SELL2:
        raise RuntimeError(
            "Размер Short изменился до выставления TP2."
        )

    short_avg = positions[SHORT_IDX]["avg"]
    b1 = Decimal(state["prices"]["B1"])

    tp2 = round_price(
        short_avg * (1 - TP2_PERCENT / 100),
        ROUND_DOWN
    )

    state["prices"]["TP2"] = fmt(tp2)
    save_state()

    log.info("ЭТАП 4: TP2=%s", tp2)

    place_once("TP2", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": "Buy",
        "orderType": "Limit",
        "qty": validate_qty(LOT_SELL2, tp2),
        "price": fmt(tp2),
        "timeInForce": "GTC",
        "positionIdx": SHORT_IDX,
        "reduceOnly": True,
        "closeOnTrigger": True
    })

    place_once("B3", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": "Buy",
        "orderType": "Limit",
        "qty": validate_qty(LOT_BUY3, b1),
        "price": fmt(b1),
        "triggerPrice": fmt(b1),
        "triggerDirection": 1,
        "triggerBy": "LastPrice",
        "timeInForce": "GTC",
        "positionIdx": LONG_IDX,
        "reduceOnly": False
    })

    set_status("STAGE4_ACTIVE")


# ============================================================
# STAGE 4 MONITOR
# ============================================================

def stage4_monitor():
    log.info("ЭТАП 4: мониторинг TP2 и B3")

    while state["status"] == "STAGE4_ACTIVE":
        winner = monitor_pair("TP2", "B3")

        if winner == "TP2":
            log.info("TP2 исполнен. Отменяю B3.")

            cancel_order("B3")

            positions = get_positions()

            if positions[SHORT_IDX]["size"] != 0:
                raise RuntimeError(
                    "После TP2 Short не равен нулю."
                )

            if positions[LONG_IDX]["size"] <= 0:
                raise RuntimeError(
                    "Неожиданный размер Long после TP2."
                )

            close_position_market(
                LONG_IDX,
                "CLOSE_LONG_AFTER_TP2"
            )

            cancel_all_strategy_orders()
            verify_flat()
            set_status("CYCLE_COMPLETED")
            return

        if winner == "B3":
            log.info("B3 исполнен. Отменяю TP2.")

            cancel_order("TP2")

            positions = get_positions()

            if positions[SHORT_IDX]["size"] != LOT_SELL2:
                raise RuntimeError(
                    "Short после B3 не равен 160 ADA."
                )

            expected_long = LOT_BUY1 + LOT_BUY3

            if positions[LONG_IDX]["size"] != expected_long:
                raise RuntimeError(
                    "Long после B3 не равен 1300 ADA."
                )

            b3_order = reconcile_order("B3", attempts=5)

            b3_avg = Decimal((b3_order or {}).get("avgPrice") or "0")
            long_avg = positions[LONG_IDX]["avg"]
            if long_avg <= 0:
                raise RuntimeError("Не удалось определить среднюю цену всей Long-позиции после B3.")

            # B3_AVG оставляем только как диагностическое значение; TP3/STOP
            # рассчитываются от weighted average всей Long-позиции (B1 + B3).
            if b3_avg > 0:
                state["prices"]["B3_AVG"] = fmt(b3_avg)
            state["prices"]["LONG_AVG_AFTER_B3"] = fmt(long_avg)

            tp3 = round_price(long_avg * (1 + TP3_PERCENT / 100), ROUND_UP)
            stop = round_price(long_avg * (1 + STOP_BUFFER_PERCENT / 100), ROUND_DOWN)

            state["prices"]["TP3"] = fmt(tp3)
            state["prices"]["STOP"] = fmt(stop)
            save_state()

            log.info(
                "Установка TP3=%s STOP=%s на весь Long",
                tp3, stop
            )

            check(
                session.set_trading_stop(
                    category=CATEGORY,
                    symbol=SYMBOL,
                    tpslMode="Full",
                    positionIdx=LONG_IDX,
                    takeProfit=fmt(tp3),
                    stopLoss=fmt(stop),
                    tpTriggerBy="LastPrice",
                    slTriggerBy="LastPrice",
                    tpOrderType="Market",
                    slOrderType="Market"
                ),
                "Set TP3/STOP"
            )

            set_status("TP3_STOP_ACTIVE")
            return



def audit_long_close_after_tp3_stop():
    """Read-only audit of recent Long-closing executions; never guesses TP/STOP."""
    import datetime
    end_ms = int(time.time() * 1000) + 5000
    start_ms = end_ms - 15 * 60 * 1000
    try:
        result = check(session.get_executions(
            category=CATEGORY, symbol=SYMBOL,
            startTime=start_ms, endTime=end_ms, limit=100
        ), "TP3/STOP execution audit")
        executions = result.get("list", [])
    except Exception as exc:
        log.error("TP3/STOP AUDIT: не удалось получить executions: %s", exc)
        return

    # Only sell executions closing the Long hedge leg; Short opening is positionIdx=2.
    rows = []
    for ex in executions:
        try:
            if int(ex.get("positionIdx") or 0) != LONG_IDX:
                continue
            if ex.get("side") != "Sell":
                continue
            qty = Decimal(str(ex.get("execQty") or "0"))
            price = Decimal(str(ex.get("execPrice") or "0"))
            if qty <= 0 or price <= 0:
                continue
            rows.append((ex, qty, price))
        except Exception:
            continue

    if not rows:
        log.warning("TP3/STOP AUDIT: закрывающие Long исполнения не найдены; причина=UNKNOWN")
        return

    # Prefer the most recent matching close fills. Fetch each order's exchange metadata.
    order_groups = {}
    for ex, qty, price in rows:
        oid = ex.get("orderId") or "UNKNOWN"
        group = order_groups.setdefault(oid, {"qty": Decimal("0"), "value": Decimal("0"),
            "fee": Decimal("0"), "pnl": Decimal("0"), "times": [], "execs": []})
        group["qty"] += qty
        group["value"] += qty * price
        group["fee"] += Decimal(str(ex.get("execFee") or "0"))
        group["pnl"] += Decimal(str(ex.get("execPnl") or "0"))
        group["times"].append(int(ex.get("execTime") or 0))
        group["execs"].append(ex)

    for oid, g in order_groups.items():
        meta = None
        if oid != "UNKNOWN":
            try:
                hist = check(session.get_order_history(category=CATEGORY, symbol=SYMBOL,
                    orderId=oid, limit=10), "TP3/STOP order metadata")
                meta = next((o for o in hist.get("list", []) if o.get("orderId") == oid), None)
            except Exception as exc:
                log.warning("TP3/STOP AUDIT: metadata orderId=%s недоступна: %s", oid, exc)
        meta = meta or {}
        stype = str(meta.get("stopOrderType") or "").lower()
        ctype = str(meta.get("createType") or "").lower()
        if "takeprofit" in stype or "takeprofit" in ctype:
            reason = "TP3"
        elif "stoploss" in stype or "stoploss" in ctype:
            reason = "STOP"
        else:
            reason = "UNKNOWN"
        avg = g["value"] / g["qty"] if g["qty"] else Decimal("0")
        times = [t for t in g["times"] if t]
        stamp = datetime.datetime.fromtimestamp(max(times)/1000).astimezone().isoformat() if times else "UNKNOWN"
        log.info("LONG CLOSE AUDIT: reason=%s symbol=%s orderId=%s qty=%s avgExecPrice=%s "
                 "fee=%s execPnl=%s execTime=%s triggerPrice=%s stopOrderType=%s createType=%s "
                 "orderStatus=%s", reason, SYMBOL, oid, g["qty"], avg, g["fee"], g["pnl"],
                 stamp, meta.get("triggerPrice"), meta.get("stopOrderType"), meta.get("createType"),
                 meta.get("orderStatus"))


# ============================================================
# TP3 / STOP MONITOR
# ============================================================

def tp3_stop_monitor():
    unknown_count = 0

    log.info("МОНИТОРИНГ TP3 / STOP")

    while state["status"] == "TP3_STOP_ACTIVE":
        try:
            positions = get_positions()
            unknown_count = 0

        except Exception as error:
            unknown_count += 1

            log.warning(
                "Ошибка чтения позиций (%s/%s): %s",
                unknown_count,
                MAX_UNKNOWN_POLLS,
                error
            )

            if unknown_count >= MAX_UNKNOWN_POLLS:
                raise RuntimeError(
                    "Потеряно подтверждение позиций. "
                    "TP3/STOP остаются на бирже. "
                    "Требуется ручная проверка."
                )

            time.sleep(POLL_SECONDS)
            continue

        long_size = positions[LONG_IDX]["size"]
        short_size = positions[SHORT_IDX]["size"]

        log.info(
            "TP3/STOP: Long=%s Short=%s",
            long_size, short_size
        )

        if long_size == 0:
            log.info("Long позиция стала нулевой. Проверяю историю исполнений Bybit; остаток Short=%s", short_size)
            audit_long_close_after_tp3_stop()

            if short_size > 0:
                close_position_market(
                    SHORT_IDX,
                    "CLOSE_SHORT_AFTER_TP3_STOP"
                )

            cancel_all_strategy_orders()
            verify_flat()
            set_status("CYCLE_COMPLETED")
            return

        time.sleep(POLL_SECONDS)


# ============================================================
# MAIN
# ============================================================

def main():
    acquire_lock()

    try:
        log.info("=" * 55)
        log.info("ADA FULL CYCLE CONTROLLER — BYBIT DEMO")
        log.info("Состояние: %s", state["status"])
        log.info("=" * 55)

        validate_account()
        recover_empty_error_state()

        while True:
            current = state["status"]

            if current == "NEW":
                stage1_b1()

            elif current == "B1_SUBMITTED":
                stage1_b1()

            elif current == "B1_FILLED":
                stage2_orders()

            elif current == "STAGE2_ACTIVE":
                stage3_monitor()

            elif current == "S2_FILLED":
                stage4_orders()

            elif current == "STAGE4_ACTIVE":
                stage4_monitor()

            elif current == "TP3_STOP_ACTIVE":
                tp3_stop_monitor()

            elif current == "CYCLE_COMPLETED":
                log.info(
                    "ЦИКЛ ЗАВЕРШЁН. Новые сделки не запускаются."
                )
                return

            elif current == "ERROR":
                raise RuntimeError(
                    "Состояние ERROR. Требуется ручная сверка."
                )

            else:
                raise RuntimeError(
                    f"Неизвестное состояние: {current}"
                )

            time.sleep(1)

    except KeyboardInterrupt:
        log.warning(
            "Остановка пользователем. "
            "Открытые позиции и заявки не отменялись."
        )

    except Exception as error:
        log.exception(
            "КРИТИЧЕСКАЯ ОШИБКА: %s",
            error
        )

        state["last_error"] = str(error)
        state["error_time"] = time.time()
        state["status"] = "ERROR"
        save_state()

        raise

    finally:
        release_lock()


if __name__ == "__main__":
    main()
