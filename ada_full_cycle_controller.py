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
# ADA 2-STEP HEDGE CONTROLLER — BYBIT DEMO
# B1 -> (TP1 | S2) -> (TP2 | STOP)
# Исправлено: TP2_LONG — стоп-маркет с триггером вниз (не лимит ниже рынка)
# Исправлено: гонка TP2 -> STOP (ErrCode 110017) — не падаем, а завершаем цикл
# Серия циклов с паузой между ними
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / "api.env")

STATE_FILE = BASE_DIR / "ada_2step_state.json"
LOCK_FILE = BASE_DIR / "ada_2step.lock"
LOG_FILE = BASE_DIR / "ada_2step.log"

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")

CATEGORY = "linear"
SYMBOL = "ADAUSDT"

# ПАРАМЕТРЫ СТРАТЕГИИ — 2 ШАГА
ZONE_PERCENT = Decimal("0.7")     # S2 = B1 * (1 - 0.7%)
TP1_PERCENT = Decimal("0.7")      # TP1 = B1 * (1 + 0.7%)
TP2_PERCENT = Decimal("0.7")      # TP2 = S2 * (1 - 0.7%)
STOP_PERCENT = Decimal("1.0")     # STOP = S2 * (1 + 1%)

LOT_BUY1 = Decimal("100")
LOT_SELL2 = Decimal("300")

LONG_IDX = 1
SHORT_IDX = 2

POLL_SECONDS = 2
RECONCILE_ATTEMPTS = 5
RECONCILE_DELAY = 1
MAX_UNKNOWN_POLLS = 30

EXPECTED_QTY = {
    "B1": Decimal("100"),
    "TP1": Decimal("100"),
    "S2": Decimal("300"),
    "TP2_SHORT": Decimal("300"),
    "TP2_LONG": Decimal("100"),
    "STOP_SHORT": Decimal("300"),
    "STOP_LONG": Decimal("100"),
}

# >>> SERIES
TF_SECONDS = 300
PAUSE_CANDLES = 2
MAX_CYCLES = 10
# <<< SERIES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

log = logging.getLogger("ADA_2STEP")

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
# SERIES: архив/сброс
# ============================================================

def archive_state(cycle_no):
    if not STATE_FILE.exists():
        return
    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive = STATE_FILE.with_name(
        f"ada_2step_state_{stamp}_cycle{cycle_no}.json"
    )
    try:
        archive.write_text(
            STATE_FILE.read_text(encoding="utf-8"),
            encoding="utf-8"
        )
        log.info("State цикла #%s архивирован: %s", cycle_no, archive.name)
    except Exception as error:
        log.warning("Не удалось архивировать state: %s", error)

def reset_state_for_next_cycle(cycle_no):
    archive_state(cycle_no)
    state.clear()
    state.update(new_state())
    save_state()
    log.info("State сброшен в NEW для следующего цикла")

# ============================================================
# LOCK
# ============================================================

def acquire_lock():
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(
            "Файл блокировки уже существует. "
            "Проверь, не работает ли другой контроллер."
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
        session.get_instruments_info(category=CATEGORY, symbol=SYMBOL),
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
        "min_value": Decimal(item["lotSizeFilter"].get("minNotionalValue", "5"))
    }

FILTERS = get_filters()

def round_price(value, rounding=ROUND_DOWN):
    value = Decimal(str(value))
    tick = FILTERS["tick"]
    return (value / tick).to_integral_value(rounding=rounding) * tick

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
# POSITIONS / ORDERS
# ============================================================

def get_positions():
    result = check(
        session.get_positions(category=CATEGORY, symbol=SYMBOL),
        "Position info"
    )
    positions = {
        LONG_IDX: {"size": Decimal("0"), "avg": Decimal("0")},
        SHORT_IDX: {"size": Decimal("0"), "avg": Decimal("0")}
    }
    for p in result.get("list", []):
        idx = int(p.get("positionIdx", 0))
        if idx in positions:
            positions[idx] = {
                "size": Decimal(p.get("size") or "0"),
                "avg": Decimal(p.get("avgPrice") or "0")
            }
    return positions

def get_open_orders():
    result = check(
        session.get_open_orders(
            category=CATEGORY, symbol=SYMBOL, openOnly=0, limit=50
        ),
        "Open orders"
    )
    return result.get("list", [])

def find_order(link_id=None, order_id=None):
    if not link_id and not order_id:
        return None
    for order in get_open_orders():
        if ((link_id and order.get("orderLinkId") == link_id)
                or (order_id and order.get("orderId") == order_id)):
            return order
    if order_id:
        result = check(
            session.get_order_history(
                category=CATEGORY, symbol=SYMBOL, orderId=order_id, limit=10
            ),
            "Order history by ID"
        )
        for order in result.get("list", []):
            if order.get("orderId") == order_id:
                return order
    if link_id:
        result = check(
            session.get_order_history(
                category=CATEGORY, symbol=SYMBOL, orderLinkId=link_id, limit=10
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
            category=CATEGORY, symbol=SYMBOL, orderId=order_id, limit=100
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
    expected = Decimal(str(item.get("requested_qty") or EXPECTED_QTY.get(name, "0")))
    if expected <= 0:
        return None
    avg_price = total_value / total_qty
    status = "Filled" if total_qty >= expected else "PartiallyFilled"
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
            found = find_order(link_id=item.get("link_id"), order_id=item.get("order_id"))
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
            log.info("Сверка %s: запись пока не найдена (%s/%s)", name, attempt, attempts)
        except Exception as error:
            log.warning("Ошибка сверки %s (%s/%s): %s", name, attempt, attempts, error)
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
            log.warning("%s: статус не получен | orderId=%s | linkId=%s",
                        name, item.get("order_id"), item.get("link_id"))
            return None
        log.info("%s: статус=%s | исполнено=%s/%s | orderId=%s",
                 name, order.get("orderStatus"),
                 order.get("cumExecQty", "0"),
                 order.get("qty", item.get("requested_qty", "?")),
                 order.get("orderId"))
        return order
    except Exception as error:
        log.exception("%s: ошибка получения статуса: %s", name, error)
        return None

def is_filled(order):
    return order is not None and order.get("orderStatus") == "Filled"

def is_partial(order):
    return order is not None and order.get("orderStatus") in (
        "PartiallyFilled", "PartiallyFilledCanceled"
    )

def is_active(order):
    return order is not None and order.get("orderStatus") in (
        "New", "Untriggered", "PartiallyFilled"
    )

# ============================================================
# SAFE ORDER SUBMISSION
# ============================================================

def place_once(name, params):
    old = state["orders"].get(name)
    if old and old.get("submitted"):
        log.info("%s: ранее уже отправлялся. Проверяю существующий ордер.", name)
        found = reconcile_order(name)
        if found:
            log.info("%s уже найден: %s", name, found.get("orderStatus"))
            return found
        if old.get("order_id"):
            log.warning("%s: orderId сохранён, статус неизвестен. "
                        "Повторная отправка запрещена.", name)
            return {
                "orderId": old["order_id"],
                "orderLinkId": old.get("link_id"),
                "orderStatus": "Unknown"
            }
        raise RuntimeError(f"{name}: предыдущая отправка не подтверждена. "
                           "Нужна ручная сверка.")
    if old:
        link_id = old["link_id"]
    else:
        link_id = "ada2" + uuid.uuid4().hex
        state["orders"][name] = {"link_id": link_id, "submitted": False}
        save_state()
    params = dict(params)
    params["orderLinkId"] = link_id
    state["orders"][name]["submitted"] = True
    state["orders"][name]["request_started"] = time.time()
    state["orders"][name]["requested_qty"] = params.get("qty")
    save_state()
    log.info("ОТПРАВКА %s | linkId=%s", name, link_id)
    try:
        response = session.place_order(**params)
        result = check(response, f"Place {name}")
        order_id = result.get("orderId")
        if not order_id:
            raise RuntimeError(f"{name}: Bybit не вернул orderId. Повтор запрещён.")
        state["orders"][name]["order_id"] = order_id
        state["orders"][name]["accepted"] = True
        save_state()
        log.info("%s принят Bybit. orderId=%s", name, order_id)
    except Exception as error:
        log.error("%s: ошибка отправки или ответа: %s", name, error)
        found = reconcile_order(name, attempts=15)
        if found:
            return found
        raise RuntimeError(f"{name}: результат отправки неизвестен. "
                           "Повторная отправка запрещена. Требуется ручная сверка.")
    found = reconcile_order(name, attempts=5)
    if found:
        return found
    log.warning("%s: успешный ответ и orderId сохранены, "
                "но история пока не показывает ордер. Повторная отправка запрещена.", name)
    return {
        "orderId": order_id,
        "orderLinkId": link_id,
        "orderStatus": "Unknown"
    }

# ============================================================
# STOP_SHORT с fallback Market -> StopMarket
# ============================================================

def place_stop_short_with_fallback():
    stop = Decimal(state["prices"]["STOP"])

    params = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": "Buy",
        "orderType": "Market",
        "qty": validate_qty(LOT_SELL2, stop),
        "triggerPrice": fmt(stop),
        "triggerDirection": 1,
        "triggerBy": "LastPrice",
        "positionIdx": SHORT_IDX,
        "reduceOnly": True
    }

    return place_once("STOP_SHORT", params)

# ============================================================
# CANCEL
# ============================================================

def cancel_order(name):
    item = state["orders"].get(name)
    if not item:
        log.warning("%s: запись для отмены отсутствует", name)
        return
    order = reconcile_order(name, attempts=5)
    if not order:
        raise RuntimeError(f"{name}: статус неизвестен. Отмена не отправлена.")
    if not is_active(order):
        log.info("%s: отмена не требуется, статус=%s", name, order.get("orderStatus"))
        return
    log.warning("ОТПРАВКА ОТМЕНЫ %s | orderId=%s", name, item.get("order_id"))
    check(
        session.cancel_order(
            category=CATEGORY, symbol=SYMBOL, orderLinkId=item["link_id"]
        ),
        f"Cancel {name}"
    )
    for attempt in range(1, 31):
        time.sleep(1)
        current = order_status(name)
        if current and current.get("orderStatus") in (
            "Cancelled", "Filled", "Deactivated", "Rejected",
            "PartiallyFilledCanceled"
        ):
            log.info("%s завершён: %s", name, current.get("orderStatus"))
            return
    raise RuntimeError(f"Не удалось подтвердить отмену {name}")

def cancel_all_strategy_orders():
    for name in list(state["orders"].keys()):
        cancel_order(name)

# ============================================================
# WAIT / POSITION CHECKS
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
                raise RuntimeError(f"{name}: статус неизвестен. Нужна ручная сверка.")
            time.sleep(POLL_SECONDS)
            continue
        unknown_count = 0
        if is_filled(order):
            log.info("%s полностью исполнен", name)
            return order
        if is_partial(order):
            raise RuntimeError(f"{name}: частичное исполнение. "
                               "Автоматический переход запрещён.")
        if order.get("orderStatus") in ("Cancelled", "Rejected", "Deactivated"):
            raise RuntimeError(f"{name}: статус {order.get('orderStatus')}")
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"Истекло время ожидания исполнения {name}")

def wait_position(idx, expected, timeout=60):
    expected = Decimal(str(expected))
    started = time.time()
    while time.time() - started < timeout:
        actual = get_positions()[idx]["size"]
        if actual == expected:
            return
        time.sleep(1)
    actual = get_positions()[idx]["size"]
    raise RuntimeError(f"Позиция idx={idx}: ожидали {expected}, получили {actual}")

def verify_flat():
    positions = get_positions()
    if positions[LONG_IDX]["size"] != 0 or positions[SHORT_IDX]["size"] != 0:
        raise RuntimeError(
            f"Счёт не flat: Long={positions[LONG_IDX]['size']} "
            f"Short={positions[SHORT_IDX]['size']}"
        )
    active = get_open_orders()
    if active:
        raise RuntimeError(f"Остались открытые ордера: {len(active)}")

# ============================================================
# ACCOUNT VALIDATION / RECOVERY
# ============================================================

def validate_account():
    log.info("Проверка DEMO аккаунта...")
    check(session.get_wallet_balance(accountType="UNIFIED"), "Wallet")
    instrument = check(
        session.get_instruments_info(category=CATEGORY, symbol=SYMBOL),
        "Instrument"
    )
    if not instrument.get("list"):
        raise RuntimeError("ADAUSDT не найден.")
    if instrument["list"][0].get("status") != "Trading":
        raise RuntimeError("ADAUSDT сейчас не торгуется.")
    positions = get_positions()
    log.info("Текущие позиции: Long=%s Short=%s",
             positions[LONG_IDX]["size"], positions[SHORT_IDX]["size"])
    if state["status"] == "NEW":
        if positions[LONG_IDX]["size"] != 0 or positions[SHORT_IDX]["size"] != 0:
            raise RuntimeError("Новый цикл запрещён: есть открытая позиция.")
        if get_open_orders():
            raise RuntimeError("Новый цикл запрещён: есть открытые ордера ADAUSDT.")

def recover_empty_error_state():
    if state.get("status") != "ERROR":
        return
    if state.get("orders"):
        raise RuntimeError("ERROR содержит записи ордеров. Автоматический сброс запрещён.")
    positions = get_positions()
    if positions[LONG_IDX]["size"] != 0 or positions[SHORT_IDX]["size"] != 0:
        raise RuntimeError("Нельзя сбросить ERROR: есть открытая позиция.")
    if get_open_orders():
        raise RuntimeError("Нельзя сбросить ERROR: есть открытые ордера.")
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
        executed_qty = Decimal(order.get("cumExecQty") or "0")
        if executed_qty != LOT_BUY1:
            raise RuntimeError(f"B1 исполнен {executed_qty}, ожидали {LOT_BUY1}")
        positions = get_positions()
        if positions[LONG_IDX]["size"] != LOT_BUY1:
            raise RuntimeError("После B1 размер Long не равен 100 ADA.")
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
    tp1 = round_price(b1 * (1 + TP1_PERCENT / 100), ROUND_UP)
    s2 = round_price(b1 * (1 - ZONE_PERCENT / 100), ROUND_DOWN)
    state["prices"]["TP1"] = fmt(tp1)
    state["prices"]["S2"] = fmt(s2)
    save_state()
    log.info("ЭТАП 2: TP1=%s | S2=%s", tp1, s2)

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
    log.info("МОНИТОРИНГ НАЧАТ: %s и %s", first_name, second_name)
    while True:
        poll_number += 1
        log.info("Проверка #%s: запрашиваю статусы %s и %s",
                 poll_number, first_name, second_name)
        first = order_status(first_name)
        second = order_status(second_name)
        first_missing = first_missing + 1 if first is None else 0
        second_missing = second_missing + 1 if second is None else 0
        if first_missing >= MAX_UNKNOWN_POLLS or second_missing >= MAX_UNKNOWN_POLLS:
            raise RuntimeError("Не удаётся подтвердить статусы ордеров. "
                               "Мониторинг остановлен для ручной сверки.")
        if is_partial(first) or is_partial(second):
            raise RuntimeError("Частичное исполнение одного из ордеров. "
                               "Автоматический переход запрещён.")
        first_filled = is_filled(first)
        second_filled = is_filled(second)
        if first_filled and second_filled:
            raise RuntimeError(f"КОНФЛИКТ: {first_name} и {second_name} оба исполнены.")
        if first_filled:
            log.warning("%s ИСПОЛНЕН", first_name)
            return first_name
        if second_filled:
            log.warning("%s ИСПОЛНЕН", second_name)
            return second_name
        for name, order in ((first_name, first), (second_name, second)):
            if order and order.get("orderStatus") in (
                "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"
            ):
                raise RuntimeError(f"{name}: неожиданный статус {order.get('orderStatus')}")
        now = time.time()
        if now - last_heartbeat >= 10:
            log.info("МОНИТОРИНГ РАБОТАЕТ | %s неизвестных проверок=%s | "
                     "%s неизвестных проверок=%s",
                     first_name, first_missing, second_name, second_missing)
            last_heartbeat = now
        time.sleep(POLL_SECONDS)

# ============================================================
# STAGE 3 — TP1 vs S2
# ============================================================

def stage3_monitor():
    log.info("ЭТАП 3: контроль TP1 и S2.")
    while state["status"] == "STAGE2_ACTIVE":
        winner = monitor_pair("TP1", "S2")
        if winner == "TP1":
            log.info("TP1 исполнен. Отменяю S2.")
            cancel_order("S2")
            positions = get_positions()
            if positions[LONG_IDX]["size"] != 0 or positions[SHORT_IDX]["size"] != 0:
                raise RuntimeError("После TP1 позиции не закрыты полностью.")
            cancel_all_strategy_orders()
            verify_flat()
            set_status("CYCLE_COMPLETED")
            return
        if winner == "S2":
            log.info("S2 исполнен. Отменяю TP1.")
            cancel_order("TP1")
            positions = get_positions()
            if positions[LONG_IDX]["size"] != LOT_BUY1:
                raise RuntimeError("После S2 размер Long не равен 100 ADA.")
            if positions[SHORT_IDX]["size"] != LOT_SELL2:
                raise RuntimeError("После S2 размер Short не равен 300 ADA.")
            set_status("S2_FILLED")
            return

# ============================================================
# STAGE 4 — TP2 / STOP (с защитой от гонки ErrCode 110017)
# ============================================================

def stage4_orders():
    if state["status"] != "S2_FILLED":
        return

    positions = get_positions()
    if positions[SHORT_IDX]["size"] != LOT_SELL2:
        raise RuntimeError("Размер Short изменился до выставления TP2/STOP.")

    short_avg = positions[SHORT_IDX]["avg"]
    s2 = Decimal(state["prices"]["S2"])
    base = short_avg if short_avg > 0 else s2

    tp2 = round_price(base * (1 - TP2_PERCENT / 100), ROUND_DOWN)
    stop = round_price(base * (1 + STOP_PERCENT / 100), ROUND_UP)

    state["prices"]["TP2"] = fmt(tp2)
    state["prices"]["STOP"] = fmt(stop)
    save_state()

    log.info("ЭТАП 4: TP2=%s | STOP=%s", tp2, stop)

    # --- TP2_SHORT: buy 300 Limit по TP2 ---
    place_once("TP2_SHORT", {
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

    # --- TP2_LONG: sell 100 Market с trigger=TP2, direction=2 (вниз) ---
    place_once("TP2_LONG", {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": "Sell",
        "orderType": "Market",
        "qty": validate_qty(LOT_BUY1, tp2),
        "triggerPrice": fmt(tp2),
        "triggerDirection": 2,
        "triggerBy": "LastPrice",
        "timeInForce": "IOC",
        "positionIdx": LONG_IDX,
        "reduceOnly": True
    })

    # >>> ПРОВЕРКА: TP2-ветка могла сработать мгновенно
    positions = get_positions()
    if positions[LONG_IDX]["size"] == 0 and positions[SHORT_IDX]["size"] == 0:
        log.info("TP2-ветка сработала сразу после выставления. Завершаю цикл.")
        cancel_all_strategy_orders()
        verify_flat()
        set_status("CYCLE_COMPLETED")
        return

    # --- STOP_SHORT: buy 300 стоп-маркет с trigger=STOP, direction=1 ---
    try:
        place_stop_short_with_fallback()
    except Exception as error:
        if "110017" in str(error):
            log.warning("STOP_SHORT: позиция уже zero (TP2 сработал). Проверяю flat.")
        else:
            raise

    # --- STOP_LONG: sell 100 Limit по STOP (выше рынка) ---
    try:
        place_once("STOP_LONG", {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "side": "Sell",
            "orderType": "Limit",
            "qty": validate_qty(LOT_BUY1, stop),
            "price": fmt(stop),
            "timeInForce": "GTC",
            "positionIdx": LONG_IDX,
            "reduceOnly": True
        })
    except Exception as error:
        if "110017" in str(error):
            log.warning("STOP_LONG: позиция уже zero (TP2 сработал). Проверяю flat.")
        else:
            raise

    # >>> ПОВТОРНАЯ ПРОВЕРКА: TP2 мог сработать, пока ставили STOP-пару
    positions = get_positions()
    if positions[LONG_IDX]["size"] == 0 and positions[SHORT_IDX]["size"] == 0:
        log.info("TP2-ветка сработала во время выставления STOP-пары. Завершаю цикл.")
        cancel_all_strategy_orders()
        verify_flat()
        set_status("CYCLE_COMPLETED")
        return

    set_status("STAGE4_ACTIVE")

# ============================================================
# STAGE 4 MONITOR — OCO между TP2-парой и STOP-парой
# ============================================================

def stage4_monitor():
    log.info("ЭТАП 4: мониторинг TP2 и STOP")
    while state["status"] == "STAGE4_ACTIVE":
        statuses = {
            name: order_status(name)
            for name in ("TP2_SHORT", "TP2_LONG", "STOP_SHORT", "STOP_LONG")
        }

        tp2_short_filled = is_filled(statuses["TP2_SHORT"])
        tp2_long_filled = is_filled(statuses["TP2_LONG"])
        stop_short_filled = is_filled(statuses["STOP_SHORT"])
        stop_long_filled = is_filled(statuses["STOP_LONG"])

        if tp2_short_filled or tp2_long_filled:
            log.info("TP2 ветка сработала. Отменяю STOP-пару.")
            cancel_order("STOP_SHORT")
            cancel_order("STOP_LONG")
            positions = get_positions()
            if positions[SHORT_IDX]["size"] > 0:
                close_position_market(SHORT_IDX, "CLOSE_SHORT_AFTER_TP2")
            if positions[LONG_IDX]["size"] > 0:
                close_position_market(LONG_IDX, "CLOSE_LONG_AFTER_TP2")
            cancel_all_strategy_orders()
            verify_flat()
            set_status("CYCLE_COMPLETED")
            return

        if stop_short_filled or stop_long_filled:
            log.info("STOP ветка сработала. Отменяю TP2-пару.")
            cancel_order("TP2_SHORT")
            cancel_order("TP2_LONG")
            positions = get_positions()
            if positions[SHORT_IDX]["size"] > 0:
                close_position_market(SHORT_IDX, "CLOSE_SHORT_AFTER_STOP")
            if positions[LONG_IDX]["size"] > 0:
                close_position_market(LONG_IDX, "CLOSE_LONG_AFTER_STOP")
            cancel_all_strategy_orders()
            verify_flat()
            set_status("CYCLE_COMPLETED")
            return

        for name, order in statuses.items():
            if order and order.get("orderStatus") in (
                "Cancelled", "Rejected", "Deactivated", "PartiallyFilledCanceled"
            ):
                positions = get_positions()
                if positions[LONG_IDX]["size"] == 0 and positions[SHORT_IDX]["size"] == 0:
                    cancel_all_strategy_orders()
                    verify_flat()
                    set_status("CYCLE_COMPLETED")
                    return
                raise RuntimeError(f"{name}: неожиданный статус {order.get('orderStatus')}")

        time.sleep(POLL_SECONDS)

# ============================================================
# CLOSE MARKET
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
# RUN ONE CYCLE
# ============================================================

def run_one_cycle():
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
        elif current == "CYCLE_COMPLETED":
            log.info("ЦИКЛ ЗАВЕРШЁН.")
            return "COMPLETED"
        elif current == "ERROR":
            log.error("Состояние ERROR. Требуется ручная сверка.")
            return "ERROR"
        else:
            raise RuntimeError(f"Неизвестное состояние: {current}")
        time.sleep(1)

# ============================================================
# MAIN
# ============================================================

def main():
    acquire_lock()
    try:
        log.info("=" * 55)
        log.info("ADA 2-STEP HEDGE CONTROLLER — BYBIT DEMO")
        log.info("Серия циклов | MAX_CYCLES=%s | пауза=%s свечей (%ss)",
                 MAX_CYCLES, PAUSE_CANDLES, PAUSE_CANDLES * TF_SECONDS)
        log.info("=" * 55)

        validate_account()
        recover_empty_error_state()

        cycle_no = 0
        while True:
            cycle_no += 1
            log.info("=" * 55)
            log.info("СТАРТ ЦИКЛА #%s | состояние=%s", cycle_no, state["status"])
            log.info("=" * 55)

            result = run_one_cycle()

            if result == "ERROR":
                log.error("Цикл #%s завершился ERROR. Серия остановлена. "
                          "Требуется ручная сверка.", cycle_no)
                return

            verify_flat()
            reset_state_for_next_cycle(cycle_no)

            if cycle_no >= MAX_CYCLES:
                log.info("Достигнут MAX_CYCLES=%s. Серия завершена.", MAX_CYCLES)
                return

            pause = PAUSE_CANDLES * TF_SECONDS
            log.info("Пауза %s свечей (%s сек) перед циклом #%s",
                     PAUSE_CANDLES, pause, cycle_no + 1)
            time.sleep(pause)

    except KeyboardInterrupt:
        log.warning("Остановка пользователем. Открытые позиции и заявки не отменялись.")

    except Exception as error:
        log.exception("КРИТИЧЕСКАЯ ОШИБКА: %s", error)
        state["last_error"] = str(error)
        state["error_time"] = time.time()
        state["status"] = "ERROR"
        save_state()
        raise

    finally:
        release_lock()

if __name__ == "__main__":
    main()
