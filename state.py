
import os
import json
import time

from config import (
    STATE_FILE,
    LOCK_FILE,
    log,
)


# ============================================================
# STATE
# ============================================================

def new_state():
    return {
        "status": "NEW",

        # Данные текущего цикла
        "cycle_id": None,
        "cycle_no": None,
        "cycle_started_at": None,

        # Ордеры и цены текущего цикла
        "orders": {},
        "prices": {},

        # Данные завершения и восстановления
        "analytics_finalized": False,
        "last_completed_cycle_id": None,
        "last_error": None,

        # Ожидание между циклами
        "wait_started_at": None,
        "wait_timeframe": "1",
        "wait_candles_required": 3,
        "wait_candles_count": 0,
        "last_closed_candle_start": None,

        "created_at": time.time()
    }


def load_state():
    if not STATE_FILE.exists():
        return new_state()

    data = json.loads(
        STATE_FILE.read_text(encoding="utf-8")
    )

    if not isinstance(data, dict):
        raise RuntimeError(
            "Файл состояния повреждён: ожидался JSON-объект."
        )

    # Миграция старого состояния.
    # Существующие значения не перезаписываем.
    defaults = new_state()

    for key, value in defaults.items():
        data.setdefault(key, value)

    if not isinstance(data["orders"], dict):
        raise RuntimeError(
            "Повреждено поле orders в файле состояния."
        )

    if not isinstance(data["prices"], dict):
        raise RuntimeError(
            "Повреждено поле prices в файле состояния."
        )

    log.info(
        "Состояние загружено: status=%s | cycle_id=%s",
        data.get("status"),
        data.get("cycle_id")
    )

    return data


state = load_state()


# ============================================================
# SAVE STATE
# ============================================================

def save_state():
    """
    Атомарная запись состояния.
    При сбое записи оригинальный файл не должен заменяться
    неполным JSON.
    """

    temp_file = STATE_FILE.with_suffix(".tmp")

    serialized = json.dumps(
        state,
        indent=2,
        ensure_ascii=False
    )

    with open(temp_file, "w", encoding="utf-8") as file:
        file.write(serialized)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp_file, STATE_FILE)


# ============================================================
# STATUS
# ============================================================

def set_status(status):
    """
    Обновляет состояние контроллера.

    Если цикл уже завершён, случайная ошибка последующей
    операции не должна стирать статус CYCLE_COMPLETED.
    """

    if (
        status == "ERROR"
        and state.get("status") == "CYCLE_COMPLETED"
    ):
        log.error(
            "Ошибка после завершения цикла. "
            "Статус CYCLE_COMPLETED сохранён."
        )
        return

    state["status"] = status
    save_state()

    log.info("СОСТОЯНИЕ: %s", status)


def set_error(error):
    """
    Записывает текст ошибки отдельно от статуса цикла.
    """

    state["last_error"] = str(error)

    if state.get("status") != "CYCLE_COMPLETED":
        state["status"] = "ERROR"

    save_state()

    log.error("ОШИБКА КОНТРОЛЛЕРА: %s", error)


def clear_last_error():
    state["last_error"] = None
    save_state()


# ============================================================
# CYCLE HELPERS
# ============================================================

def has_active_cycle():
    return bool(state.get("cycle_id"))


def get_cycle_id():
    return state.get("cycle_id")


def reset_cycle_data():
    """
    Сбрасывает данные ордеров только для нового цикла.

    ВНИМАНИЕ:
    Вызывать только после подтверждения Bybit:
    Long = 0, Short = 0, открытых ордеров = 0,
    а предыдущий цикл уже сохранён в аналитике.
    """

    if state.get("status") != "CYCLE_COMPLETED":
        raise RuntimeError(
            "Нельзя сбрасывать данные: предыдущий цикл "
            "не имеет статуса CYCLE_COMPLETED."
        )

    if not state.get("analytics_finalized"):
        raise RuntimeError(
            "Нельзя начинать новый цикл: аналитика "
            "предыдущего цикла ещё не подтверждена."
        )

    previous_cycle_id = state.get("cycle_id")

    state["last_completed_cycle_id"] = previous_cycle_id

    state["cycle_id"] = None
    state["cycle_no"] = None
    state["cycle_started_at"] = None

    state["orders"] = {}
    state["prices"] = {}

    state["analytics_finalized"] = False
    state["last_error"] = None

    state["wait_started_at"] = None
    state["wait_candles_count"] = 0
    state["last_closed_candle_start"] = None

    state["status"] = "NEW"

    save_state()

    log.info(
        "Данные предыдущего цикла %s архивированы. "
        "Подготовлено состояние нового цикла.",
        previous_cycle_id
    )


# ============================================================
# LOCK
# ============================================================

def acquire_lock():
    """
    Запрещает запуск второго экземпляра контроллера.
    Существующий lock автоматически не удаляется.
    """

    try:
        fd = os.open(
            str(LOCK_FILE),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY
        )

        try:
            lock_data = (
                f"PID={os.getpid()}\n"
                f"TIME={time.time()}\n"
            )

            os.write(
                fd,
                lock_data.encode("utf-8")
            )

            os.fsync(fd)

        finally:
            os.close(fd)
            return True

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

        log.info("Файл блокировки освобождён.")

    except Exception as error:
        log.warning(
            "Не удалось удалить lock-файл: %s",
            error
        )
