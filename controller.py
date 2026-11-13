
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

from config import (
    session,
    CATEGORY,
    SYMBOL,
    ZONE_PERCENT,
    TP1_PERCENT,
    TP2_PERCENT,
    TP3_PERCENT,
    STOP_BUFFER_PERCENT,
    LOT_BUY1,
    LOT_SELL2,
    LOT_BUY3,
    LONG_IDX,
    SHORT_IDX,
    POLL_SECONDS,
    MAX_UNKNOWN_POLLS,
)

from state import (
    state,
    save_state,
    set_status,
    set_error,
    clear_last_error,
    new_state,
    acquire_lock,
    release_lock,
    reset_cycle_data,
)

from exchange import (
    check,
    round_price,
    fmt,
    get_position,
    get_position_qty,
    get_open_orders,
    get_order_executions,
    summarize_executions,
    reconcile_order,
    is_partial,
    is_active,
    place_once,
    cancel_order,
    cancel_all_strategy_orders,
    wait_filled,
    wait_position,
    verify_flat,
    close_position_market,
    validate_account,
)

from analytics import (
    begin_cycle,
    add_executions,
    finish_cycle,
    log_summary,
)

log = logging.getLogger("ada_controller")


# ============================================================
# Общие функции
# ============================================================

def D(value) -> Decimal:
    return Decimal(str(value or "0"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_stage(status: str) -> None:
    set_status(status)
    save_state()


def cycle_id() -> str:
    value = state.get("cycle_id")
    if not value:
        raise RuntimeError("Отсутствует cycle_id")
    return value


def require_flat() -> None:
    if not verify_flat():
        raise RuntimeError(
            "Bybit не подтвердил отсутствие позиций и открытых ордеров"
        )


def require_position(position_idx: int, expected) -> None:
    actual = get_position_qty(position_idx)
    if actual != D(expected):
        raise RuntimeError(
            f"Позиция {position_idx}: ожидалось {expected}, получено {actual}"
        )


def order_item(name: str) -> dict:
    item = state.get("orders", {}).get(name)
    if not item:
        raise RuntimeError(f"Ордер {name} отсутствует в state")
    return item


# ============================================================
# Сбор исполнений для аналитики
# ============================================================

def record_executions(name: str, item: dict) -> list[dict]:
    order_id = item.get("orderId")

    if not order_id:
        raise RuntimeError(
            f"{name}: нет подтверждённого orderId"
        )

    executions = get_order_executions(order_id=str(order_id))

    if not executions:
        result = reconcile_order(name, item)

        if D(result.get("cumExecQty")) > 0:
            raise RuntimeError(
                f"{name}: Bybit сообщает исполнение, "
                "но список executions пуст"
            )

        return []

    add_executions(cycle_id(), executions)

    summary = summarize_executions(executions)

    item["actual_exec_qty"] = fmt(summary["qty"])
    item["actual_avg_price"] = fmt(summary["average_price"])
    item["actual_exec_fee"] = fmt(summary["fee"])
    item["actual_first_exec_time"] = summary["first_exec_time"]
    item["actual_last_exec_time"] = summary["last_exec_time"]

    save_state()
    return executions


def collect_all_executions() -> list[dict]:
    """
    Повторно запрашивает исполнения всех ордеров текущего цикла.
    add_executions() устраняет дубли по execId.
    """
    combined = []

    for name, item in state.get("orders", {}).items():
        if not item.get("orderId"):
            raise RuntimeError(
                f"{name}: ордер сохранён без orderId; "
                "полнота аналитики не подтверждена"
            )

        combined.extend(record_executions(name, item))

    return combined


# ============================================================
# Рыночные цены и целевые уровни
# ============================================================

def last_price() -> Decimal:
    result = check(
        session.get_tickers(
            category=CATEGORY,
            symbol=SYMBOL,
        ),
        "get_tickers",
    )

    rows = result.get("list", [])
    if not rows:
        raise RuntimeError("Не получена рыночная цена")

    price = D(rows[0].get("lastPrice"))
    if price <= 0:
        raise RuntimeError("Некорректная рыночная цена")

    return price


def buy_target(entry: Decimal, percent: Decimal) -> Decimal:
    return round_price(
        entry * (Decimal("1") + percent / Decimal("100")),
        "up",
    )


def sell_target(entry: Decimal, percent: Decimal) -> Decimal:
    return round_price(
        entry * (Decimal("1") - percent / Decimal("100")),
        "down",
    )


# ============================================================
# Размещение ордеров
# ============================================================

def submit_b1() -> dict:
    item = place_once(
        name="B1",
        side="Buy",
        order_type="Market",
        qty=LOT_BUY1,
        position_idx=LONG_IDX,
    )
    set_stage("B1_SUBMITTED")
    return item


def submit_tp1(entry: Decimal) -> dict:
    item = place_once(
        name="TP1",
        side="Sell",
        order_type="Limit",
        qty=LOT_BUY1,
        position_idx=LONG_IDX,
        price=buy_target(entry, TP1_PERCENT),
        reduce_only=True,
    )
    save_state()
    return item


def submit_s2(entry: Decimal) -> dict:
    item = place_once(
        name="S2",
        side="Sell",
        order_type="Market",
        qty=LOT_SELL2,
        position_idx=SHORT_IDX,
        trigger_price=sell_target(entry, ZONE_PERCENT),
        trigger_direction=2,
    )
    save_state()
    return item


def submit_tp2(short_entry: Decimal) -> dict:
    item = place_once(
        name="TP2",
        side="Buy",
        order_type="Limit",
        qty=LOT_SELL2,
        position_idx=SHORT_IDX,
        price=sell_target(short_entry, TP2_PERCENT),
        reduce_only=True,
    )
    save_state()
    return item


def submit_b3(long_entry: Decimal) -> dict:
    item = place_once(
        name="B3",
        side="Buy",
        order_type="Market",
        qty=LOT_BUY3,
        position_idx=LONG_IDX,
        trigger_price=buy_target(long_entry, ZONE_PERCENT),
        trigger_direction=1,
    )
    save_state()
    return item


def submit_tp3_stop(long_average: Decimal) -> None:
    tp3_price = buy_target(long_average, TP3_PERCENT)

    stop_price = round_price(
        long_average * (
            Decimal("1") + STOP_BUFFER_PERCENT / Decimal("100")
        ),
        "down",
    )

    place_once(
        name="TP3",
        side="Sell",
        order_type="Limit",
        qty=LOT_BUY1 + LOT_BUY3,
        position_idx=LONG_IDX,
        price=tp3_price,
        reduce_only=True,
    )

    place_once(
        name="STOP",
        side="Sell",
        order_type="Market",
        qty=LOT_BUY1 + LOT_BUY3,
        position_idx=LONG_IDX,
        trigger_price=stop_price,
        trigger_direction=2,
        reduce_only=True,
    )

    state.setdefault("prices", {})["TP3"] = fmt(tp3_price)
    state["prices"]["STOP"] = fmt(stop_price)
    save_state()


# ============================================================
# Мониторинг двух конкурирующих ордеров
# ============================================================

def monitor_pair(first_name: str, second_name: str) -> str:
    unknown = 0

    while unknown < MAX_UNKNOWN_POLLS:
        first = order_item(first_name)
        second = order_item(second_name)

        a = reconcile_order(first_name, first)
        b = reconcile_order(second_name, second)

        a_status = a.get("orderStatus")
        b_status = b.get("orderStatus")

        a_qty = D(a.get("cumExecQty"))
        b_qty = D(b.get("cumExecQty"))

        if is_partial(a_status) or is_partial(b_status):
            raise RuntimeError(
                f"Частичное исполнение пары: "
                f"{first_name}={a_status}, {second_name}={b_status}"
            )

        if a_status == "Filled" and b_status == "Filled":
            raise RuntimeError(
                f"Исполнены оба ордера: {first_name} и {second_name}"
            )

        if a_status == "Filled":
            if a_qty <= 0:
                raise RuntimeError(f"{first_name}: Filled без количества")
            record_executions(first_name, first)
            return first_name

        if b_status == "Filled":
            if b_qty <= 0:
                raise RuntimeError(f"{second_name}: Filled без количества")
            record_executions(second_name, second)
            return second_name

        if not a.get("verified") or not b.get("verified"):
            unknown += 1
        else:
            unknown = 0

        time.sleep(POLL_SECONDS)

    raise RuntimeError(
        f"Не удалось достоверно определить победителя "
        f"{first_name}/{second_name}"
    )


def cancel_loser(name: str) -> None:
    item = order_item(name)

    cancel_order(name, item)

    result = reconcile_order(name, item)
    status = result.get("orderStatus")

    if status == "Filled":
        raise RuntimeError(
            f"{name} успел исполниться во время отмены"
        )

    if is_partial(status):
        raise RuntimeError(
            f"{name} частично исполнился во время отмены"
        )

    record_executions(name, item)


# ============================================================
# Завершение цикла
# ============================================================

def finalize_cycle(outcome: str) -> None:
    if outcome not in {"TP1", "TP2", "TP3", "STOP", "UNKNOWN"}:
        raise ValueError(f"Недопустимый outcome: {outcome}")

    # Повторно получаем все исполнения перед фиксацией результата.
    executions = collect_all_executions()

    require_flat()

    result = finish_cycle(
        cycle_id=cycle_id(),
        outcome=outcome,
        executions=executions,
    )

    state["cycle_outcome"] = outcome
    state["analytics_finalized"] = True
    state["analytics_finished_at"] = utc_now()
    state["analytics_pnl_verified"] = bool(
        result.get("pnl_verified")
    )

    save_state()
    log_summary()


# ============================================================
# Стадия B1
# ============================================================

def stage_b1() -> None:
    item = submit_b1()
    result = wait_filled("B1", item)

    if result.get("orderStatus") != "Filled":
        raise RuntimeError("B1 не исполнен полностью")

    record_executions("B1", item)

    position = wait_position(LONG_IDX, LOT_BUY1)
    entry = D(position.get("avgPrice"))

    if entry <= 0:
        raise RuntimeError("Не определена средняя цена B1")

    state.setdefault("prices", {})["B1"] = fmt(entry)

    submit_tp1(entry)
    submit_s2(entry)

    set_stage("WAIT_TP1_OR_S2")


# ============================================================
# Стадия TP1 / S2
# ============================================================

def stage_tp1_s2() -> None:
    winner = monitor_pair("TP1", "S2")

    if winner == "TP1":
        cancel_loser("S2")
        require_flat()
        finalize_cycle("TP1")
        set_stage("CYCLE_COMPLETED")
        return

    if winner == "S2":
        cancel_loser("TP1")

        require_position(LONG_IDX, LOT_BUY1)
        require_position(SHORT_IDX, LOT_SELL2)

        short_position = get_position(SHORT_IDX)
        short_entry = D(short_position.get("avgPrice"))

        if short_entry <= 0:
            raise RuntimeError("Не определена средняя цена Short")

        long_position = get_position(LONG_IDX)
        long_entry = D(long_position.get("avgPrice"))

        if long_entry <= 0:
            raise RuntimeError("Не определена средняя цена Long")

        state.setdefault("prices", {})["S2"] = fmt(short_entry)
        save_state()

        submit_tp2(short_entry)
        submit_b3(long_entry)

        set_stage("WAIT_TP2_OR_B3")
        return

    raise RuntimeError(f"Неизвестный победитель: {winner}")


# ============================================================
# Стадия TP2 / B3
# ============================================================

def close_long_after_tp2() -> None:
    qty = get_position_qty(LONG_IDX)

    if qty <= 0:
        return

    item = close_position_market(
        position_idx=LONG_IDX,
        side="Sell",
        qty=qty,
    )

    if not item:
        raise RuntimeError("Не удалось создать закрывающий Long ордер")

    state.setdefault("orders", {})["CLOSE_LONG_TP2"] = item
    save_state()

    result = wait_filled("CLOSE_LONG_TP2", item)

    if result.get("orderStatus") != "Filled":
        raise RuntimeError("Закрытие Long после TP2 не подтверждено")

    record_executions("CLOSE_LONG_TP2", item)


def stage_tp2_b3() -> None:
    winner = monitor_pair("TP2", "B3")

    if winner == "TP2":
        cancel_loser("B3")
        close_long_after_tp2()

        cancel_all_strategy_orders()
        require_flat()
        finalize_cycle("TP2")
        set_stage("CYCLE_COMPLETED")
        return

    if winner == "B3":
        cancel_loser("TP2")

        require_position(
            LONG_IDX,
            D(LOT_BUY1) + D(LOT_BUY3),
        )
        require_position(SHORT_IDX, LOT_SELL2)

        long_position = get_position(LONG_IDX)
        long_average = D(long_position.get("avgPrice"))

        if long_average <= 0:
            raise RuntimeError("Не определена средняя цена Long после B3")

        state.setdefault("prices", {})["LONG_AFTER_B3"] = fmt(long_average)
        save_state()

        submit_tp3_stop(long_average)
        set_stage("WAIT_TP3_OR_STOP")
        return

    raise RuntimeError(f"Неизвестный победитель: {winner}")


# ============================================================
# Стадия TP3 / STOP
# ============================================================

def close_short_after_long_exit() -> None:
    qty = get_position_qty(SHORT_IDX)

    if qty <= 0:
        return

    item = close_position_market(
        position_idx=SHORT_IDX,
        side="Buy",
        qty=qty,
    )

    if not item:
        raise RuntimeError("Не удалось создать закрывающий Short ордер")

    state.setdefault("orders", {})["CLOSE_SHORT_FINAL"] = item
    save_state()

    result = wait_filled("CLOSE_SHORT_FINAL", item)

    if result.get("orderStatus") != "Filled":
        raise RuntimeError("Закрытие Short не подтверждено")

    record_executions("CLOSE_SHORT_FINAL", item)


def stage_tp3_stop() -> None:
    winner = monitor_pair("TP3", "STOP")

    cancel_loser("STOP" if winner == "TP3" else "TP3")

    if get_position_qty(LONG_IDX) != 0:
        raise RuntimeError(
            f"{winner} исполнен, но Long ещё не закрыт"
        )

    close_short_after_long_exit()

    cancel_all_strategy_orders()
    require_flat()

    finalize_cycle(winner)
    set_stage("CYCLE_COMPLETED")


# ============================================================
# Минутные свечи: ожидание трёх последующих закрытий
# ============================================================

def server_time_ms() -> int:
    result = check(
        session.get_server_time(),
        "get_server_time",
    )

    if result.get("timeNano"):
        return int(result["timeNano"]) // 1_000_000

    if result.get("timeSecond"):
        return int(result["timeSecond"]) * 1000

    raise RuntimeError("Не получено серверное время Bybit")


def get_closed_candle_starts() -> list[int]:
    result = check(
        session.get_kline(
            category=CATEGORY,
            symbol=SYMBOL,
            interval="1",
            limit=20,
        ),
        "get_kline",
    )

    now = server_time_ms()
    closed = []

    for candle in result.get("list", []):
        start = int(candle[0])

        if start + 60_000 <= now:
            closed.append(start)

    return sorted(set(closed))


def start_candle_wait() -> None:
    closed = get_closed_candle_starts()

    if not closed:
        raise RuntimeError("Нет данных о закрытых минутных свечах")

    state["wait_started_at"] = utc_now()
    state["wait_timeframe"] = "1"
    state["wait_candles_required"] = 3
    state["wait_candles_count"] = 0

    # Последняя уже закрытая свеча исключается из трёх последующих.
    state["last_closed_candle_start"] = closed[-1]

    set_stage("WAIT_THREE_CANDLES")


def wait_three_closed_candles() -> None:
    baseline = state.get("last_closed_candle_start")

    if baseline is None:
        raise RuntimeError("Не сохранена базовая свеча ожидания")

    baseline = int(baseline)

    while True:
        closed = get_closed_candle_starts()

        subsequent = [
            candle for candle in closed
            if candle > baseline
        ]

        state["wait_candles_count"] = min(len(subsequent), 3)

        if subsequent:
            state["last_closed_candle_start"] = subsequent[-1]

        save_state()

        if len(subsequent) >= 3:
            return

        time.sleep(2)


# ============================================================
# Обработка завершённого цикла
# ============================================================

def handle_completed_cycle() -> None:
    require_flat()

    if not state.get("analytics_finalized"):
        outcome = state.get("cycle_outcome") or "UNKNOWN"
        finalize_cycle(outcome)

    if not state.get("wait_started_at"):
        start_candle_wait()

    wait_three_closed_candles()

    # Проверка повторяется после ожидания.
    require_flat()

    reset_cycle_data()
    save_state()


# ============================================================
# Запуск нового цикла
# ============================================================

def begin_new_cycle() -> None:
    require_flat()

    begin_cycle(state, save_state)

    clear_last_error()
    save_state()

    log.info(
        "Старт цикла №%s, cycle_id=%s",
        state.get("cycle_no"),
        state.get("cycle_id"),
    )

    stage_b1()


# ============================================================
# Восстановление и диспетчер стадий
# ============================================================

def recover_state() -> None:
    status = state.get("status")

    if status == "ERROR":
        raise RuntimeError(
            "Контроллер находится в ERROR. "
            "Автоматическое продолжение запрещено."
        )

    if status == "NEW":
        if state.get("cycle_id"):
            raise RuntimeError(
                "Статус NEW, но остался cycle_id"
            )
        require_flat()


def run_stage() -> None:
    status = state.get("status")

    if status == "NEW":
        begin_new_cycle()

    elif status == "B1_SUBMITTED":
        item = order_item("B1")
        result = wait_filled("B1", item)

        if result.get("orderStatus") != "Filled":
            raise RuntimeError("B1 не исполнен")

        record_executions("B1", item)

        position = wait_position(LONG_IDX, LOT_BUY1)
        entry = D(position.get("avgPrice"))

        if entry <= 0:
            raise RuntimeError("Не определена цена B1")

        state.setdefault("prices", {})["B1"] = fmt(entry)
        submit_tp1(entry)
        submit_s2(entry)
        set_stage("WAIT_TP1_OR_S2")

    elif status == "WAIT_TP1_OR_S2":
        stage_tp1_s2()

    elif status == "WAIT_TP2_OR_B3":
        stage_tp2_b3()

    elif status == "WAIT_TP3_OR_STOP":
        stage_tp3_stop()

    elif status in {"CYCLE_COMPLETED", "WAIT_THREE_CANDLES"}:
        handle_completed_cycle()

    else:
        raise RuntimeError(f"Неизвестный статус: {status}")


# ============================================================
# Главная функция
# ============================================================

def main() -> None:
    if not acquire_lock():
        raise RuntimeError(
            "Не удалось получить lock: возможно, контроллер уже запущен"
        )

    try:
        validate_account()
        recover_state()

        log.info("ADAUSDT DEMO controller запущен")

        while True:
            try:
                run_stage()

            except KeyboardInterrupt:
                log.warning("Остановка контроллера пользователем")
                raise

            except Exception as exc:
                log.exception("Ошибка контроллера: %s", exc)

                if state.get("status") not in {
                    "CYCLE_COMPLETED",
                    "WAIT_THREE_CANDLES",
                }:
                    set_error(str(exc))

                save_state()
                raise

    finally:
        release_lock()


if __name__ == "__main__":
    main()
