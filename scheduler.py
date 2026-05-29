import sys
import json
import threading
import schedule
import time
import requests
from datetime import datetime
from main import (run_full_analysis, run_manage_only, init_exchange,
                  get_exchange_positions, send_telegram, load_json, save_json,
                  TRADE_LOG_FILE, POSITIONS_FILE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

STATE_FILE = "bot_state.json"
bot_active = True


def load_state():
    state = load_json(STATE_FILE, {"active": True})
    return state.get("active", True)


def save_state(active):
    save_json(STATE_FILE, {"active": active})


def close_all_positions():
    """모든 포지션 청산"""
    exchange = init_exchange()
    positions = get_exchange_positions(exchange)
    if not positions:
        return "보유 포지션 없음"

    closed = []
    trade_log = load_json(TRADE_LOG_FILE, {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []})

    for sym, pos in positions.items():
        try:
            close_side = "sell" if pos["side"] == "long" else "buy"
            exchange.create_market_order(sym, close_side, pos["amount"], params={"reduceOnly": True})
            coin = sym.split("/")[0]
            pnl = round(pos["unrealized_pnl"], 2)
            pnl_pct = (pnl / pos["notional"] * 100) if pos["notional"] else 0

            if pnl >= 0:
                trade_log["wins"] += 1
                closed.append(f"  ✅ {coin}: +${pnl:.1f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)")
            else:
                trade_log["losses"] += 1
                closed.append(f"  ❌ {coin}: ${pnl:.1f} ({pnl_pct:.1f}%)")
            trade_log["total_pnl"] = round(trade_log["total_pnl"] + pnl, 2)
        except Exception as e:
            closed.append(f"  {sym}: 청산 실패 ({e})")

    save_json(TRADE_LOG_FILE, trade_log)

    # 열린 주문(스톱로스 등)도 취소
    try:
        open_orders = exchange.fetch_open_orders()
        for order in open_orders:
            exchange.cancel_order(order["id"], order["symbol"])
    except Exception:
        pass

    # positions.json 초기화
    save_json(POSITIONS_FILE, {})
    return "\n".join(closed)


def close_position(coin):
    """개별 포지션 청산"""
    exchange = init_exchange()
    positions = get_exchange_positions(exchange)
    managed = load_json(POSITIONS_FILE, {})

    # 코인명으로 심볼 찾기
    target_sym = None
    for sym in positions:
        if sym.split("/")[0] == coin:
            target_sym = sym
            break

    if not target_sym:
        return f"❌ {coin} 포지션 없음"

    pos = positions[target_sym]
    try:
        close_side = "sell" if pos["side"] == "long" else "buy"
        exchange.create_market_order(target_sym, close_side, pos["amount"], params={"reduceOnly": True})

        pnl = round(pos["unrealized_pnl"], 2)
        pnl_pct = (pnl / pos["notional"] * 100) if pos["notional"] else 0

        trade_log = load_json(TRADE_LOG_FILE, {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []})
        if pnl >= 0:
            trade_log["wins"] += 1
            emoji = "✅"
        else:
            trade_log["losses"] += 1
            emoji = "❌"
        trade_log["total_pnl"] = round(trade_log["total_pnl"] + pnl, 2)
        save_json(TRADE_LOG_FILE, trade_log)

        # 스톱로스 주문 취소
        try:
            open_orders = exchange.fetch_open_orders(target_sym)
            for order in open_orders:
                exchange.cancel_order(order["id"], target_sym)
        except Exception:
            pass

        # positions.json에서 제거
        if target_sym in managed:
            del managed[target_sym]
            save_json(POSITIONS_FILE, managed)

        return f"{emoji} {coin} 청산: ${'+' if pnl >= 0 else ''}{pnl:.1f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)"
    except Exception as e:
        return f"❌ {coin} 청산 실패: {e}"


def get_status():
    """현재 상태 요약"""
    exchange = init_exchange()
    balance = exchange.fetch_balance()
    usdt_total = float(balance["USDT"]["total"])
    positions = get_exchange_positions(exchange)
    total_unrealized = sum(p["unrealized_pnl"] for p in positions.values())
    trade_log = load_json(TRADE_LOG_FILE, {"wins": 0, "losses": 0, "total_pnl": 0.0})

    lines = [f"📊 상태 조회",
             f"💰 ${usdt_total:,.0f} | 미실현 ${'+' if total_unrealized >= 0 else ''}{total_unrealized:.1f}",
             f"📈 {trade_log['wins']}승 {trade_log['losses']}패 (${'+' if trade_log['total_pnl'] >= 0 else ''}{trade_log['total_pnl']:.1f})",
             f"🤖 봇: {'ON 🟢' if bot_active else 'OFF 🔴'}",
             f"📋 포지션: {len(positions)}개"]

    for sym, pos in positions.items():
        coin = sym.split("/")[0]
        side_kr = "롱" if pos["side"] == "long" else "숏"
        pnl = pos["unrealized_pnl"]
        pnl_pct = (pnl / pos["notional"] * 100) if pos["notional"] else 0
        lines.append(f"  {'🟢' if pos['side'] == 'long' else '🔴'} {coin} {side_kr} ${pos['notional']:,.0f} (${'+' if pnl >= 0 else ''}{pnl:.1f} / {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}%)")

    return "\n".join(lines)


def poll_telegram():
    """텔레그램 명령어 폴링"""
    global bot_active
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    last_update_id = 0

    while True:
        try:
            resp = requests.get(url, params={"offset": last_update_id + 1, "timeout": 30}, timeout=35)
            if resp.status_code != 200:
                time.sleep(5)
                continue

            updates = resp.json().get("result", [])
            for update in updates:
                last_update_id = update["update_id"]
                msg = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower()

                if chat_id != str(TELEGRAM_CHAT_ID):
                    continue

                if text == "/off":
                    bot_active = False
                    save_state(False)
                    result = close_all_positions()
                    send_telegram(f"🔴 봇 OFF\n모든 포지션 청산:\n{result}\n\nMac 꺼도 됩니다.")

                elif text == "/on":
                    bot_active = True
                    save_state(True)
                    send_telegram("🟢 봇 ON\n매매 재개합니다.")

                elif text == "/status":
                    send_telegram(get_status())

                elif text.startswith("/close"):
                    parts = text.split()
                    if len(parts) < 2:
                        exchange = init_exchange()
                        pos = get_exchange_positions(exchange)
                        if not pos:
                            send_telegram("보유 포지션 없음")
                        else:
                            coins = [s.split("/")[0] for s in pos]
                            send_telegram("청산할 종목을 선택하세요:\n" + "\n".join(f"  /close {c}" for c in coins) + "\n  /close all - 전체 청산")
                    elif parts[1] == "all":
                        result = close_all_positions()
                        send_telegram(f"전체 청산:\n{result}")
                    else:
                        coin = parts[1].upper()
                        result = close_position(coin)
                        send_telegram(result)

                elif text == "/help":
                    send_telegram("📌 명령어\n/on - 봇 시작\n/off - 전체 청산 + 봇 정지\n/close BTC - 개별 청산\n/close all - 전체 청산\n/status - 현재 상태\n/help - 명령어 목록")

        except Exception as e:
            print(f"텔레그램 폴링 에러: {e}")
            time.sleep(5)


def job_manage():
    if not bot_active:
        return
    print(f"\n[{datetime.now()}] 포지션 관리 실행")
    try:
        run_manage_only()
    except Exception as e:
        print(f"포지션 관리 에러: {e}")


def job_full():
    if not bot_active:
        print(f"\n[{datetime.now()}] 봇 OFF 상태 - 스킵")
        return
    print(f"\n[{datetime.now()}] 전체 분석 실행")
    try:
        run_full_analysis()
    except Exception as e:
        print(f"전체 분석 에러: {e}")


# 매 정시 포지션 관리 (전체 분석 시간 제외)
for h in ["01:00", "02:00", "03:00", "05:00", "06:00", "07:00",
          "09:00", "10:00", "11:00", "13:00", "14:00", "15:00",
          "17:00", "18:00", "19:00", "21:00", "22:00", "23:00"]:
    schedule.every().day.at(h).do(job_manage)

# 전체 분석 (0, 4, 8, 12, 16, 20시)
for h in ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]:
    schedule.every().day.at(h).do(job_full)

# 봇 상태 로드
bot_active = load_state()

# 텔레그램 폴링 스레드 시작
telegram_thread = threading.Thread(target=poll_telegram, daemon=True)
telegram_thread.start()

print(f"=== 봇 스케줄러 시작 ({datetime.now()}) ===")
print(f"상태: {'ON' if bot_active else 'OFF'}")
print("스케줄: 매 1시간 포지션 관리 / 매 4시간 전체 분석")
print("명령어: /on /off /close /status /help")

send_telegram(f"🤖 봇 스케줄러 시작\n상태: {'🟢 ON' if bot_active else '🔴 OFF'}\n/help 로 명령어 확인")

while True:
    schedule.run_pending()
    time.sleep(30)
