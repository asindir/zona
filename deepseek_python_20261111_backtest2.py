import csv

# ============================================================
# НАСТРОЙКИ (2-STEP HEDGE на 5-минутках)
# ============================================================

INPUT_FILE = "ADAUSDT_5m.csv"

ZONE_PERCENT = 0.7

TP1_PERCENT = 0.7       # Подняли TP1
TP2_PERCENT = 0.7       # Подняли TP2

LOT_BUY1 = 1000.0
LOT_SELL2 = 3000.0      # Снизили лот S2

STOP_PERCENT = 1      # Подняли стоп


# ============================================================
# ЗАГРУЗКА CSV
# ============================================================

def load_candles(filename):
    candles = []
    with open(filename, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            candles.append({
                "timestamp": int(row["timestamp"]),
                "datetime": row["datetime_utc"],
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"])
            })
    candles.sort(key=lambda x: x["timestamp"])
    return candles


# ============================================================
# P/L
# ============================================================

def pnl_tp1(buy1, exit_price):
    return LOT_BUY1 * (exit_price - buy1)

def pnl_tp2(buy1, sell2, exit_price):
    return LOT_BUY1 * (exit_price - buy1) + LOT_SELL2 * (sell2 - exit_price)

def pnl_stop(buy1, sell2, exit_price):
    return LOT_BUY1 * (exit_price - buy1) + LOT_SELL2 * (sell2 - exit_price)


# ============================================================
# BACKTEST (С ЭМУЛЯЦИЕЙ ПУТИ ЦЕНЫ ВНУТРИ 5-МИНУТНОЙ СВЕЧИ)
# ============================================================

def run_backtest(candles):
    trades = []
    cycle = 0
    i = 0
    total_pnl = 0.0

    tp1_count = 0
    tp2_count = 0
    stop_count = 0

    while i < len(candles):
        entry_candle = candles[i]
        cycle += 1
        buy1 = entry_candle["open"]

        sell2 = buy1 * (1.0 - ZONE_PERCENT / 100.0)
        tp1 = buy1 * (1.0 + TP1_PERCENT / 100.0)
        tp2 = sell2 * (1.0 - TP2_PERCENT / 100.0)
        stop = sell2 * (1.0 + STOP_PERCENT / 100.0)

        entry_time = entry_candle["datetime"]
        stage = 1
        s2_time = ""
        exit_found = False
        j = i

        while j < len(candles):
            candle = candles[j]
            high = candle["high"]
            low = candle["low"]
            is_bullish = candle["close"] > candle["open"]

            # =================================================
            # STAGE 1 — Только B1
            # =================================================
            if stage == 1:
                hit_s2 = low <= sell2
                hit_tp1 = high >= tp1

                if hit_s2 and hit_tp1:
                    if is_bullish:
                        stage = 2
                        s2_time = candle["datetime"]
                        if high >= stop:
                            exit_price = stop
                            pnl = pnl_stop(buy1, sell2, exit_price)
                            total_pnl += pnl; stop_count += 1
                            trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": s2_time, "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": "STOP", "pnl": pnl})
                            exit_found = True; i = j + 1; break
                    else:
                        exit_price = tp1
                        pnl = pnl_tp1(buy1, exit_price)
                        total_pnl += pnl; tp1_count += 1
                        trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": "", "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": "TP1", "pnl": pnl})
                        exit_found = True; i = j + 1; break

                elif hit_s2:
                    stage = 2
                    s2_time = candle["datetime"]
                    if high >= stop:
                        exit_price = stop
                        pnl = pnl_stop(buy1, sell2, exit_price)
                        total_pnl += pnl; stop_count += 1
                        trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": s2_time, "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": "STOP", "pnl": pnl})
                        exit_found = True; i = j + 1; break
                    j += 1; continue

                elif hit_tp1:
                    exit_price = tp1
                    pnl = pnl_tp1(buy1, exit_price)
                    total_pnl += pnl; tp1_count += 1
                    trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": "", "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": "TP1", "pnl": pnl})
                    exit_found = True; i = j + 1; break
                else:
                    j += 1; continue

            # =================================================
            # STAGE 2 — B1 + S2
            # =================================================
            elif stage == 2:
                hit_tp2 = low <= tp2
                hit_stop = high >= stop

                if hit_stop and hit_tp2:
                    if is_bullish:
                        exit_price = tp2; pnl = pnl_tp2(buy1, sell2, exit_price); tp2_count += 1; result = "TP2"
                    else:
                        exit_price = stop; pnl = pnl_stop(buy1, sell2, exit_price); stop_count += 1; result = "STOP"
                    total_pnl += pnl
                    trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": s2_time, "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": result, "pnl": pnl})
                    exit_found = True; i = j + 1; break

                elif hit_stop:
                    exit_price = stop; pnl = pnl_stop(buy1, sell2, exit_price); total_pnl += pnl; stop_count += 1
                    trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": s2_time, "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": "STOP", "pnl": pnl})
                    exit_found = True; i = j + 1; break

                elif hit_tp2:
                    exit_price = tp2; pnl = pnl_tp2(buy1, sell2, exit_price); total_pnl += pnl; tp2_count += 1
                    trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": s2_time, "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": candle["datetime"], "exit_price": exit_price, "result": "TP2", "pnl": pnl})
                    exit_found = True; i = j + 1; break
                else:
                    j += 1; continue

        if not exit_found:
            trades.append({"cycle": cycle, "entry_time": entry_time, "entry_price": buy1, "s2_time": s2_time, "s2_price": sell2, "tp1": tp1, "tp2": tp2, "stop": stop, "exit_time": "", "exit_price": "", "result": "OPEN", "pnl": 0.0})
            break

    return trades, total_pnl, tp1_count, tp2_count, stop_count


# ============================================================
# ВЫВОД ВСЕХ СДЕЛОК
# ============================================================

def print_trades(trades):
    print()
    print("=" * 110)
    print("ВСЕ СДЕЛКИ (ДЕТАЛЬНО)")
    print("=" * 110)
    print(f"{'№':>4} | {'ENTRY TIME':<20} | {'S2 TIME':<20} | {'EXIT TIME':<20} | {'RESULT':<8} | {'P/L':>10}")
    print("-" * 110)
    
    for t in trades:
        s2_time_str = t['s2_time'] if t['s2_time'] else '-'
        exit_time_str = t['exit_time'] if t['exit_time'] else '-'
        
        print(
            f"{t['cycle']:>4} | "
            f"{t['entry_time']:<20} | "
            f"{s2_time_str:<20} | "
            f"{exit_time_str:<20} | "
            f"{t['result']:<8} | "
            f"{t['pnl']:>10.2f}"
        )
    print("=" * 110)


# ============================================================
# СТАТИСТИКА
# ============================================================

def print_statistics(candles, trades, total_pnl, tp1_count, tp2_count, stop_count):
    closed = [t for t in trades if t["result"] != "OPEN"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]

    print()
    print("=" * 75)
    print("2-STEP ZONARECOVERY BACKTEST (5 MIN DATA)")
    print("=" * 75)
    print(f"Файл:              {INPUT_FILE}")
    print(f"Свечей:            {len(candles)}")
    print(f"Zone:              {ZONE_PERCENT:.2f}%")
    print(f"TP1:               +{TP1_PERCENT:.2f}%")
    print(f"TP2:               -{TP2_PERCENT:.2f}% от S2")
    print(f"STOP:              +{STOP_PERCENT:.2f}% от S2")
    print(f"B1 lot:            {LOT_BUY1:.2f}")
    print(f"S2 lot:            {LOT_SELL2:.2f}")
    print("-" * 75)
    print(f"Всего циклов:      {len(trades)}")
    print(f"TP1:               {tp1_count}")
    print(f"TP2:               {tp2_count}")
    print(f"STOP:              {stop_count}")
    print(f"ИТОГО P/L:         {total_pnl:.2f}")

    if closed:
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = sum(t["pnl"] for t in losses)
        print(f"Win rate:          {len(wins) / len(closed) * 100:.2f}%")
        if gross_loss != 0:
            print(f"Profit factor:     {gross_profit / abs(gross_loss):.3f}")
    print("=" * 75)


# ============================================================
# ЗАПУСК
# ============================================================

if __name__ == "__main__":
    print("Загрузка данных...")
    candles = load_candles(INPUT_FILE)
    if not candles:
        print("ERROR: CSV пустой.")
        raise SystemExit(1)

    trades, total_pnl, tp1, tp2, stops = run_backtest(candles)
    
    # Выводим статистику
    print_statistics(candles, trades, total_pnl, tp1, tp2, stops)
    
    # Выводим каждую сделку
    print_trades(trades)