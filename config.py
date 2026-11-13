
import logging
from pathlib import Path
from decimal import Decimal


# ============================================================
# ПУТИ И НАСТРОЙКИ ТЕСТА
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = BASE_DIR / "ada_full_cycle_state.json"
LOCK_FILE = BASE_DIR / "ada_full_cycle.lock"
LOG_FILE = BASE_DIR / "ada_full_cycle_test.log"

TEST_MODE = True
HISTORICAL_CSV = BASE_DIR / "ADAUSDT_5m.csv"


# ============================================================
# ПАРАМЕТРЫ ИНСТРУМЕНТА
# ============================================================

CATEGORY = "linear"
SYMBOL = "ADAUSDT"


# ============================================================
# ПАРАМЕТРЫ СТРАТЕГИИ — НЕ ИЗМЕНЕНЫ
# ============================================================

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


# ============================================================
# МОНИТОРИНГ И СВЕРКА ОРДЕРОВ
# ============================================================

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


# ============================================================
# ПАРАМЕТРЫ ИСТОРИЧЕСКОГО ЭМУЛЯТОРА
# ============================================================

# Эти значения временные. Их нужно сверить
# с реальными параметрами инструмента Bybit.
PRICE_TICK = Decimal("0.0001")
QTY_STEP = Decimal("1")
MIN_QTY = Decimal("1")

# Комиссия пока отключена.
# Для проверки механики она не влияет на переходы состояний.
FEE_RATE = Decimal("0")


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

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
# ТЕСТОВАЯ СЕССИЯ
# ============================================================

# Реального подключения к Bybit в тестовом config.py нет.
session = None
