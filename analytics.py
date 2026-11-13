
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


log = logging.getLogger("ada_controller")

BASE_DIR = Path(__file__).resolve().parent
ANALYTICS_FILE = BASE_DIR / "ada_cycles.json"

VALID_OUTCOMES = {
    "TP1",
    "TP2",
    "TP3",
    "STOP",
    "UNKNOWN",
}


# ============================================================
# Общие функции
# ============================================================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dec(value: Any) -> Decimal | None:
    if value is None:
        return None

    try:
        result = Decimal(str(value))
        if not result.is_finite():
            return None
        return result
    except (InvalidOperation, ValueError, TypeError):
        return None


def decimal_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def empty_ledger() -> dict:
    return {
        "version": 2,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "cycles": [],
    }


# ============================================================
# Чтение и сохранение журнала
# ============================================================

def load_ledger() -> dict:
    if not ANALYTICS_FILE.exists():
        return empty_ledger()

    try:
        with ANALYTICS_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            raise ValueError("Корневой объект журнала должен быть dict")

        if not isinstance(data.get("cycles"), list):
            raise ValueError("В журнале отсутствует список cycles")

        data.setdefault("version", 2)
        data.setdefault("created_at", now_iso())
        data.setdefault("updated_at", now_iso())

        return data

    except Exception as exc:
        # Не создаём пустой журнал поверх повреждённого:
        # это могло бы уничтожить историю сделок.
        raise RuntimeError(
            f"Не удалось прочитать {ANALYTICS_FILE}: {exc}"
        ) from exc


def save_ledger(ledger: dict) -> None:
    ledger["updated_at"] = now_iso()

    ANALYTICS_FILE.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_path = tempfile.mkstemp(
        prefix="ada_cycles_",
        suffix=".tmp",
        dir=str(ANALYTICS_FILE.parent),
    )

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                ledger,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temp_path, ANALYTICS_FILE)

    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def find_cycle(ledger: dict, cycle_id: str) -> dict | None:
    for cycle in ledger["cycles"]:
        if cycle.get("cycle_id") == cycle_id:
            return cycle
    return None


# ============================================================
# Начало цикла
# ============================================================

def begin_cycle(state: dict, save_state) -> str:
    """
    Создаёт запись цикла.

    Порядок:
    1. Сохраняет cycle_id в state.
    2. Сохраняет RUNNING в журнал.
    3. При повторном вызове с тем же cycle_id не создаёт дубль.
    """
    existing_id = state.get("cycle_id")

    ledger = load_ledger()

    if existing_id:
        existing = find_cycle(ledger, existing_id)

        if existing:
            if existing.get("status") == "COMPLETED":
                raise RuntimeError(
                    f"Цикл {existing_id} уже завершён"
                )
            return existing_id

        # Восстановление после сбоя между записью state и журнала.
        # Номер цикла восстанавливается из текущей истории.
        cycle_no = len(ledger["cycles"]) + 1

        ledger["cycles"].append({
            "cycle_id": existing_id,
            "cycle_no": cycle_no,
            "status": "RUNNING",
            "outcome": None,
            "started_at": state.get("cycle_started_at") or now_iso(),
            "finished_at": None,
            "executions": [],
            "gross_pnl": None,
            "fees": None,
            "net_pnl": None,
            "pnl_verified": False,
            "recovered": True,
        })

        save_ledger(ledger)
        return existing_id

    cycle_id = str(uuid.uuid4())
    cycle_no = len(ledger["cycles"]) + 1
    started_at = now_iso()

    state["cycle_id"] = cycle_id
    state["cycle_no"] = cycle_no
    state["cycle_started_at"] = started_at
    state["analytics_finalized"] = False

    # Сначала сохраняем идентификатор в state.
    save_state()

    ledger["cycles"].append({
        "cycle_id": cycle_id,
        "cycle_no": cycle_no,
        "status": "RUNNING",
        "outcome": None,
        "started_at": started_at,
        "finished_at": None,
        "executions": [],
        "gross_pnl": None,
        "fees": None,
        "net_pnl": None,
        "pnl_verified": False,
    })

    save_ledger(ledger)
    return cycle_id


# ============================================================
# Нормализация исполнений
# ============================================================

def normalize_execution(execution: dict) -> dict:
    return {
        "exec_id": execution.get("execId"),
        "order_id": execution.get("orderId"),
        "order_link_id": execution.get("orderLinkId"),
        "side": execution.get("side"),
        "position_idx": execution.get("positionIdx"),
        "qty": str(execution.get("execQty", "0")),
        "price": str(execution.get("execPrice", "0")),
        "fee": str(execution.get("execFee", "0")),
        "time": execution.get("execTime"),
        "type": execution.get("execType"),
    }


def merge_executions(
    existing: list[dict],
    incoming: list[dict],
) -> list[dict]:
    """
    Объединяет исполнения без дублирования по execId.
    """
    merged = {}
    no_id = []

    for execution in existing + incoming:
        exec_id = execution.get("exec_id") or execution.get("execId")

        if exec_id:
            merged[str(exec_id)] = execution
        else:
            no_id.append(execution)

    result = list(merged.values()) + no_id
    result.sort(
        key=lambda item: int(
            item.get("time") or item.get("execTime") or 0
        )
    )

    return result


def add_executions(
    cycle_id: str,
    executions: list[dict],
) -> None:
    ledger = load_ledger()
    cycle = find_cycle(ledger, cycle_id)

    if not cycle:
        raise RuntimeError(
            f"Цикл {cycle_id} отсутствует в аналитическом журнале"
        )

    normalized = [
        normalize_execution(item)
        for item in executions
    ]

    cycle["executions"] = merge_executions(
        cycle.get("executions", []),
        normalized,
    )

    save_ledger(ledger)


# ============================================================
# Расчёт финансового результата
# ============================================================

def calculate_cycle_pnl(
    executions: list[dict],
) -> dict:
    """
    Расчёт по фактическим исполнениям.

    Важное ограничение:
    для точного P/L нужно знать, какие исполнения открывали
    и закрывали Long и Short. Поэтому функция рассчитывает
    результат только при корректной последовательности
    исполнений и известной позиции.
    """
    positions = {
        1: Decimal("0"),
        2: Decimal("0"),
    }

    average_cost = {
        1: Decimal("0"),
        2: Decimal("0"),
    }

    realized = Decimal("0")
    fees = Decimal("0")
    valid = True

    for execution in executions:
        side = execution.get("side")
        position_idx = int(execution.get("position_idx") or 0)

        qty = dec(execution.get("qty"))
        price = dec(execution.get("price"))
        fee = dec(execution.get("fee"))

        if (
            position_idx not in positions
            or side not in {"Buy", "Sell"}
            or qty is None
            or price is None
            or fee is None
            or qty <= 0
            or price <= 0
        ):
            valid = False
            continue

        fees += fee

        signed_qty = qty if side == "Buy" else -qty
        current_qty = positions[position_idx]
        current_avg = average_cost[position_idx]

        # Позиция открывается или увеличивается в том же направлении.
        if current_qty == 0 or (
            current_qty > 0 and signed_qty > 0
        ) or (
            current_qty < 0 and signed_qty < 0
        ):
            new_qty = current_qty + signed_qty

            if current_qty == 0:
                average_cost[position_idx] = price
            else:
                old_abs = abs(current_qty)
                add_abs = abs(signed_qty)

                average_cost[position_idx] = (
                    old_abs * current_avg + add_abs * price
                ) / (old_abs + add_abs)

            positions[position_idx] = new_qty
            continue

        # Закрывается существующая позиция.
        close_qty = min(abs(current_qty), abs(signed_qty))

        if current_qty > 0:
            realized += close_qty * (price - current_avg)
        else:
            realized += close_qty * (current_avg - price)

        new_qty = current_qty + signed_qty

        if new_qty == 0:
            average_cost[position_idx] = Decimal("0")

        elif (
            current_qty > 0 and new_qty < 0
        ) or (
            current_qty < 0 and new_qty > 0
        ):
            # Переворот позиции одним исполнением.
            # Цена нового остатка равна цене этого исполнения.
            average_cost[position_idx] = price

        positions[position_idx] = new_qty

    flat = all(qty == 0 for qty in positions.values())

    if not flat:
        valid = False

    net = realized - fees

    return {
        "gross_pnl": realized if valid else None,
        "fees": fees if valid else None,
        "net_pnl": net if valid else None,
        "pnl_verified": valid,
        "positions_at_end": {
            str(key): str(value)
            for key, value in positions.items()
        },
    }


# ============================================================
# Завершение цикла
# ============================================================

def finish_cycle(
    cycle_id: str,
    outcome: str,
    exit_price=None,
    gross_pnl=None,
    fees=None,
    net_pnl=None,
    pnl_verified: bool = False,
    executions: list[dict] | None = None,
    exit_time=None,
    exit_qty=None,
    exit_reason: str | None = None,
) -> dict:
    """
    Завершает цикл.

    Если переданы исполнения, P/L пересчитывается из них.
    Если P/L не подтверждён, финансовые значения остаются null.
    """
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"Недопустимый outcome: {outcome}")

    ledger = load_ledger()
    cycle = find_cycle(ledger, cycle_id)

    if not cycle:
        raise RuntimeError(
            f"Цикл {cycle_id} отсутствует в журнале"
        )

    if cycle.get("status") == "COMPLETED":
        return cycle

    if executions:
        normalized = [
            normalize_execution(item)
            for item in executions
        ]

        cycle["executions"] = merge_executions(
            cycle.get("executions", []),
            normalized,
        )

    final_executions = cycle.get("executions", [])

    calculated = calculate_cycle_pnl(final_executions)

    # Автоматически подтверждаем P/L только если расчёт
    # выполнен по полным исполнениям и позиции закрыты.
    if calculated["pnl_verified"]:
        gross = calculated["gross_pnl"]
        total_fees = calculated["fees"]
        net = calculated["net_pnl"]
        verified = True
    elif pnl_verified:
        gross = dec(gross_pnl)
        total_fees = dec(fees)
        net = dec(net_pnl)

        verified = (
            gross is not None
            and total_fees is not None
            and net is not None
            and gross - total_fees == net
        )
    else:
        gross = None
        total_fees = None
        net = None
        verified = False

    cycle.update({
        "status": "COMPLETED",
        "outcome": outcome,
        "finished_at": now_iso(),
        "exit_time": exit_time,
        "exit_price": (
            decimal_string(dec(exit_price))
            if exit_price is not None
            else None
        ),
        "exit_qty": (
            decimal_string(dec(exit_qty))
            if exit_qty is not None
            else None
        ),
        "exit_reason": exit_reason or outcome,
        "gross_pnl": decimal_string(gross),
        "fees": decimal_string(total_fees),
        "net_pnl": decimal_string(net),
        "pnl_verified": verified,
        "pnl_source": (
            "bybit_executions"
            if calculated["pnl_verified"]
            else "externally_verified"
            if verified
            else "unverified"
        ),
    })

    save_ledger(ledger)
    return cycle


# ============================================================
# Статистика
# ============================================================

def get_summary() -> dict:
    ledger = load_ledger()

    completed = [
        cycle for cycle in ledger["cycles"]
        if cycle.get("status") == "COMPLETED"
    ]

    verified = [
        cycle for cycle in completed
        if cycle.get("pnl_verified")
        and dec(cycle.get("net_pnl")) is not None
    ]

    wins = [
        cycle for cycle in verified
        if dec(cycle["net_pnl"]) > 0
    ]

    losses = [
        cycle for cycle in verified
        if dec(cycle["net_pnl"]) < 0
    ]

    gross_values = [
        dec(cycle["gross_pnl"])
        for cycle in verified
    ]

    fee_values = [
        dec(cycle["fees"])
        for cycle in verified
    ]

    net_values = [
        dec(cycle["net_pnl"])
        for cycle in verified
    ]

    gross_total = sum(gross_values, Decimal("0"))
    fees_total = sum(fee_values, Decimal("0"))
    net_total = sum(net_values, Decimal("0"))

    count = len(verified)

    average_net = (
        net_total / count
        if count
        else None
    )

    best = max(net_values) if net_values else None
    worst = min(net_values) if net_values else None

    win_rate = (
        Decimal(len(wins)) * Decimal("100") / count
        if count
        else None
    )

    outcomes = {
        outcome: sum(
            1 for cycle in completed
            if cycle.get("outcome") == outcome
        )
        for outcome in VALID_OUTCOMES
    }

    return {
        "total_cycles": len(ledger["cycles"]),
        "completed_cycles": len(completed),
        "running_cycles": sum(
            1 for cycle in ledger["cycles"]
            if cycle.get("status") == "RUNNING"
        ),
        "verified_cycles": count,
        "unverified_cycles": len(completed) - count,
        "wins": len(wins),
        "losses": len(losses),
        "break_even": count - len(wins) - len(losses),
        "win_rate_percent": (
            decimal_string(win_rate)
            if win_rate is not None
            else None
        ),
        "gross_pnl_total": decimal_string(gross_total),
        "fees_total": decimal_string(fees_total),
        "net_pnl_total": decimal_string(net_total),
        "average_net_pnl": decimal_string(average_net),
        "best_net_pnl": decimal_string(best),
        "worst_net_pnl": decimal_string(worst),
        "outcomes": outcomes,
    }


def log_summary() -> dict:
    summary = get_summary()

    log.info(
        "ANALYTICS | cycles=%s completed=%s verified=%s "
        "unverified=%s wins=%s losses=%s win_rate=%s%% "
        "gross=%s fees=%s net=%s average=%s best=%s worst=%s",
        summary["total_cycles"],
        summary["completed_cycles"],
        summary["verified_cycles"],
        summary["unverified_cycles"],
        summary["wins"],
        summary["losses"],
        summary["win_rate_percent"],
        summary["gross_pnl_total"],
        summary["fees_total"],
        summary["net_pnl_total"],
        summary["average_net_pnl"],
        summary["best_net_pnl"],
        summary["worst_net_pnl"],
    )

    return summary
