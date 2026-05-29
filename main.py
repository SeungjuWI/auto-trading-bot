import os
import json
import time
import ccxt
import requests
import pandas as pd
import ta
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ── 전략 설정 ──
SCAN_TOP_N = 20           # 거래량 상위 스캔 수
SELECT_N = 6              # AI 종목 선정 수
POSITION_RATIO = 0.05     # 종목당 잔고의 5%
MAX_TOTAL_EXPOSURE = 0.60 # 전체 노출 한도 60%
PARTIAL_TP_RATIO = 0.5    # 1차 익절 시 50% 정리
TRAIL_ACTIVATION_RR = 2.0 # R:R 2배 도달 시 트레일링 활성화
TRAIL_CALLBACK_ATR = 1.5  # 트레일링 콜백: ATR 1.5배
TRADE_LOG_FILE = "trade_log.json"
POSITIONS_FILE = "positions.json"


# ══════════════════════════════════════
#  시스템 프롬프트
# ══════════════════════════════════════

SCREENING_PROMPT = """너는 가상화폐 선물 시장의 종목 스크리너야.
아래에 바이낸스 선물 거래량 상위 코인들의 24시간 데이터가 주어진다.
이 중에서 단기 매매(롱 또는 숏)에 가장 유리한 종목을 정확히 {select_n}개 골라라.

[선정 기준]
1. 24시간 거래량이 충분히 높아 유동성이 보장되는 종목
2. 24시간 변동률이 +-2% 이상이되 +-15% 이상 과변동 종목은 제외
3. 명확한 추세 또는 반전 시그널이 보이는 종목 우선
4. BTC, ETH는 기본 포함
5. 스테이블코인이나 레버리지 토큰은 제외

반드시 JSON으로만 응답:
selected: 종목 배열 (예: ["BTC/USDT", "ETH/USDT", ...])
reasoning: 선정 이유 (간략히)"""

ANALYSIS_PROMPT = """너는 철저한 리스크 관리와 누적 수익을 최우선으로 하는 보수적인 가상화폐 퀀트 트레이더야.

[시장 상태 정보]
- 시장 상태: {market_regime}
- 펀딩비: {funding_rate}% (양수=롱과열, 음수=숏과열)

[매매 원칙]
1. 최우선 목표는 '원금 보존'. 추세가 불분명하면 반드시 HOLD.
2. 4시간봉의 추세 방향으로만 진입. 역추세 매매 금지.
3. 확신도 80 이상일 때만 진입. 레버리지 최대 3배.
4. 횡보장(SIDEWAYS)에서는 무조건 HOLD.
5. 펀딩비가 +-0.1% 이상이면 과열 방향 반대 포지션에 가점.
6. 이미 포지션이 있으면 같은 방향 추가 진입 금지.

[손절/익절 기준]
- 손절: ATR 1.5배 (데이터에 atr_4h 제공됨)
- 1차 익절: 손절폭의 2배(R:R 1:2) 도달 시 50% 정리
- 나머지 50%: 트레일링 스탑으로 추세 끝까지 보유

반드시 JSON으로만 응답:
analysis: 지표 요약 및 근거 (간략히)
confidence_score: 0~100
action: "BUY" | "SELL" | "HOLD"
leverage: 1~3
stop_loss_atr_multiplier: 1.0~2.0 (ATR 몇 배를 손절로 쓸지)
reasoning_tags: 시그널 태그 배열 (예: ["MACD_BULL", "RSI_OVERSOLD", "TREND_UP", "FUNDING_EXTREME"])"""


# ══════════════════════════════════════
#  유틸리티
# ══════════════════════════════════════

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})
    if resp.status_code == 200:
        print("텔레그램 전송 성공")
    else:
        print(f"텔레그램 전송 실패: {resp.text}")


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def init_exchange():
    exchange = ccxt.binance({
        "apiKey": BINANCE_API_KEY,
        "secret": BINANCE_SECRET_KEY,
        "options": {"defaultType": "future"},
    })
    exchange.enable_demo_trading(True)
    return exchange


# ══════════════════════════════════════
#  시장 스캔 & 종목 선정
# ══════════════════════════════════════

def scan_market(exchange):
    tickers = exchange.fetch_tickers()
    usdt_futures = []
    for symbol, t in tickers.items():
        if "/USDT:USDT" not in symbol:
            continue
        quote_vol = float(t.get("quoteVolume") or 0)
        pct_change = float(t.get("percentage") or 0)
        if quote_vol > 0:
            clean_symbol = symbol.replace(":USDT", "")
            usdt_futures.append({
                "symbol": clean_symbol,
                "price": t.get("last"),
                "change_24h_pct": round(pct_change, 2),
                "volume_usdt": round(quote_vol),
            })

    usdt_futures.sort(key=lambda x: x["volume_usdt"], reverse=True)
    candidates = usdt_futures[:SCAN_TOP_N]
    print(f"거래량 상위 {len(candidates)}개 스캔 완료")

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SCREENING_PROMPT.format(select_n=SELECT_N)},
            {"role": "user", "content": json.dumps(candidates, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    result = json.loads(response.choices[0].message.content)
    print(f"AI 종목 선정: {result['selected']}")
    return result["selected"], result["reasoning"]


# ══════════════════════════════════════
#  기술적 분석 (멀티 타임프레임)
# ══════════════════════════════════════

def fetch_multi_tf_indicators(exchange, symbol):
    """4시간봉(추세) + 1시간봉(진입 타이밍) 분석"""
    data = {}
    for tf, label in [("4h", "4h"), ("1h", "1h")]:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

        # 핵심 지표
        df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=14).rsi()
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()
        bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_middle"] = bb.bollinger_mavg()
        df["bb_lower"] = bb.bollinger_lband()

        # ATR (손절/익절 계산용)
        df["atr"] = ta.volatility.AverageTrueRange(df["high"], df["low"], df["close"], window=14).average_true_range()

        # EMA 추세 (20/50)
        df["ema_20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()
        df["ema_50"] = ta.trend.EMAIndicator(df["close"], window=50).ema_indicator()

        data[label] = df

    return data


def detect_market_regime(df_4h):
    """4시간봉 기준 시장 상태 분류"""
    recent = df_4h.tail(10)
    ema_20 = recent["ema_20"].iloc[-1]
    ema_50 = recent["ema_50"].iloc[-1]
    atr = recent["atr"].iloc[-1]
    close = recent["close"].iloc[-1]

    # ATR 대비 가격 변동률로 변동성 판단
    atr_pct = (atr / close) * 100

    # EMA 정배열/역배열
    if ema_20 > ema_50 * 1.005:
        trend = "UPTREND"
    elif ema_20 < ema_50 * 0.995:
        trend = "DOWNTREND"
    else:
        trend = "SIDEWAYS"

    # 고변동 시장 감지
    if atr_pct > 3.0:
        trend = "HIGH_VOLATILITY"

    return trend


def get_funding_rate(exchange, symbol):
    """현재 펀딩비 조회"""
    try:
        funding = exchange.fetch_funding_rate(symbol)
        rate = float(funding.get("fundingRate") or 0) * 100
        return round(rate, 4)
    except Exception:
        return 0.0


# ══════════════════════════════════════
#  AI 분석
# ══════════════════════════════════════

def ask_ai(symbol, tf_data, position_info, market_regime, funding_rate):
    cols = ["timestamp", "open", "high", "low", "close", "volume",
            "rsi", "macd", "macd_signal", "macd_hist",
            "bb_upper", "bb_middle", "bb_lower", "atr", "ema_20", "ema_50"]

    # 4시간봉 최근 5개 + 1시간봉 최근 5개
    candles = {}
    for tf in ["4h", "1h"]:
        recent = tf_data[tf].tail(5)[cols].copy()
        recent["timestamp"] = recent["timestamp"].astype(str)
        candles[tf] = recent.round(4).to_dict(orient="records")

    pos_text = "현재 포지션 없음"
    if position_info:
        pos_text = (f"현재 포지션: {position_info['side']} {position_info['amount']}개, "
                    f"진입가 {position_info['entry_price']}, 미실현 손익 ${position_info['unrealized_pnl']}")

    atr_4h = tf_data["4h"]["atr"].iloc[-1]

    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": ANALYSIS_PROMPT.format(
                market_regime=market_regime,
                funding_rate=funding_rate,
            )},
            {"role": "user", "content": (
                f"종목: {symbol}\n"
                f"atr_4h: {atr_4h:.4f}\n"
                f"{pos_text}\n\n"
                f"4시간봉 최근 5개:\n{json.dumps(candles['4h'], ensure_ascii=False)}\n\n"
                f"1시간봉 최근 5개:\n{json.dumps(candles['1h'], ensure_ascii=False)}"
            )},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    return json.loads(response.choices[0].message.content)


# ══════════════════════════════════════
#  포지션 관리 (트레일링 스탑 + 분할 익절)
# ══════════════════════════════════════

def load_managed_positions():
    return load_json(POSITIONS_FILE, {})


def save_managed_positions(positions):
    save_json(POSITIONS_FILE, positions)


def manage_existing_positions(exchange, managed_positions):
    """기존 포지션에 대해 트레일링 스탑 / 분할 익절 관리"""
    exchange_positions = get_exchange_positions(exchange)
    actions_taken = []
    trade_log = load_json(TRADE_LOG_FILE, {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []})

    for symbol, managed in list(managed_positions.items()):
        if symbol not in exchange_positions:
            # 포지션이 청산됨 → 승패 기록
            coin = symbol.split("/")[0]
            # 청산 시 실현 손익 추정 (마지막 가격 기준)
            try:
                ticker = exchange.fetch_ticker(symbol)
                last_price = ticker["last"]
                qty = managed.get("quantity", 0)
                if managed["side"] == "buy":
                    pnl = (last_price - managed["entry_price"]) * qty
                else:
                    pnl = (managed["entry_price"] - last_price) * qty
                pnl = round(pnl, 2)
            except Exception:
                pnl = 0.0

            if pnl >= 0:
                trade_log["wins"] += 1
                actions_taken.append(f"  ✅ {coin}: 청산 +${pnl:.1f}")
            else:
                trade_log["losses"] += 1
                actions_taken.append(f"  ❌ {coin}: 청산 ${pnl:.1f}")
            trade_log["total_pnl"] = round(trade_log["total_pnl"] + pnl, 2)
            save_json(TRADE_LOG_FILE, trade_log)
            del managed_positions[symbol]
            continue

        pos = exchange_positions[symbol]
        entry_price = managed["entry_price"]
        stop_loss = managed["stop_loss"]
        atr = managed["atr_4h"]
        current_price = float(pos.get("markPrice") or pos["entry_price"])
        side = managed["side"]
        initial_risk = abs(entry_price - stop_loss)

        # 현재 수익 계산
        if side == "buy":
            current_pnl_r = (current_price - entry_price) / initial_risk if initial_risk else 0
        else:
            current_pnl_r = (entry_price - current_price) / initial_risk if initial_risk else 0

        # 1차 익절: R:R 2배 도달 시 50% 정리
        if not managed.get("partial_tp_done") and current_pnl_r >= TRAIL_ACTIVATION_RR:
            amount = pos["amount"]
            close_qty = float(exchange.amount_to_precision(symbol, amount * PARTIAL_TP_RATIO))
            if close_qty > 0:
                close_side = "sell" if side == "buy" else "buy"
                try:
                    exchange.create_market_order(symbol, close_side, close_qty, params={"reduceOnly": True})
                    managed["partial_tp_done"] = True
                    coin = symbol.split("/")[0]
                    actions_taken.append(f"  \U0001f4b0 {coin}: 1차 익절 50% ({current_pnl_r:.1f}R)")
                except Exception as e:
                    print(f"  분할 익절 실패 {symbol}: {e}")

        # 트레일링 스탑 업데이트
        if current_pnl_r >= TRAIL_ACTIVATION_RR:
            trail_callback = atr * TRAIL_CALLBACK_ATR
            if side == "buy":
                new_stop = current_price - trail_callback
                if new_stop > managed["stop_loss"]:
                    managed["stop_loss"] = round(new_stop, 4)
                    actions_taken.append(f"  \u2191 {symbol.split('/')[0]}: 트레일링 SL \u2192 ${new_stop:,.2f}")
            else:
                new_stop = current_price + trail_callback
                if new_stop < managed["stop_loss"]:
                    managed["stop_loss"] = round(new_stop, 4)
                    actions_taken.append(f"  \u2193 {symbol.split('/')[0]}: 트레일링 SL \u2192 ${new_stop:,.2f}")

        # 현재 가격이 스탑로스에 도달하면 청산
        if side == "buy" and current_price <= managed["stop_loss"]:
            try:
                exchange.create_market_order(symbol, "sell", pos["amount"], params={"reduceOnly": True})
                pnl = (current_price - entry_price) * pos["amount"]
                actions_taken.append(f"  \U0001f6d1 {symbol.split('/')[0]}: 스탑 청산 (${pnl:+.2f})")
                del managed_positions[symbol]
            except Exception as e:
                print(f"  스탑 청산 실패 {symbol}: {e}")
        elif side == "sell" and current_price >= managed["stop_loss"]:
            try:
                exchange.create_market_order(symbol, "buy", pos["amount"], params={"reduceOnly": True})
                pnl = (entry_price - current_price) * pos["amount"]
                actions_taken.append(f"  \U0001f6d1 {symbol.split('/')[0]}: 스탑 청산 (${pnl:+.2f})")
                del managed_positions[symbol]
            except Exception as e:
                print(f"  스탑 청산 실패 {symbol}: {e}")

    return actions_taken


def get_exchange_positions(exchange):
    positions = exchange.fetch_positions()
    active = {}
    for p in positions:
        amt = float(p["contracts"] or 0)
        if amt > 0:
            active[p["symbol"]] = {
                "side": p["side"],
                "amount": amt,
                "entry_price": float(p["entryPrice"] or 0),
                "unrealized_pnl": round(float(p["unrealizedPnl"] or 0), 2),
                "notional": abs(float(p["notional"] or 0)),
                "markPrice": p.get("markPrice"),
            }
    return active


# ══════════════════════════════════════
#  주문 실행
# ══════════════════════════════════════

def execute_trade(exchange, symbol, decision, current_positions, usdt_total, atr_4h):
    action = decision["action"]
    confidence = decision.get("confidence_score", 0)

    if action == "HOLD" or confidence < 80:
        return None

    # 이미 같은 방향 포지션 보유 시 스킵
    if symbol in current_positions:
        existing_side = current_positions[symbol]["side"]
        if (action == "BUY" and existing_side == "long") or (action == "SELL" and existing_side == "short"):
            print(f"  이미 {existing_side} 포지션 보유 → 스킵")
            return None

    # 전체 노출 한도
    total_exposure = sum(p["notional"] for p in current_positions.values())
    if total_exposure >= usdt_total * MAX_TOTAL_EXPOSURE:
        print(f"  전체 노출 한도 초과 → 스킵")
        return None

    leverage = min(decision.get("leverage", 1), 3)
    sl_atr_mult = decision.get("stop_loss_atr_multiplier", 1.5)

    exchange.set_leverage(leverage, symbol)

    balance = exchange.fetch_balance()
    usdt_available = float(balance["USDT"]["free"])
    order_amount_usdt = usdt_available * POSITION_RATIO
    ticker = exchange.fetch_ticker(symbol)
    current_price = ticker["last"]

    quantity = order_amount_usdt * leverage / current_price
    quantity = float(exchange.amount_to_precision(symbol, quantity))
    market = exchange.market(symbol)
    min_qty = market["limits"]["amount"]["min"]

    if quantity < min_qty:
        print(f"  최소 주문 수량 미달 → 스킵")
        return None

    side = "buy" if action == "BUY" else "sell"
    order = exchange.create_market_order(symbol, side, quantity)
    print(f"  {side.upper()} {quantity} @ market (x{leverage})")

    # ATR 기반 동적 손절
    atr_stop = atr_4h * sl_atr_mult
    if action == "BUY":
        stop_price = round(current_price - atr_stop, 4)
    else:
        stop_price = round(current_price + atr_stop, 4)

    # 거래소에 스톱마켓 주문
    sl_side = "sell" if action == "BUY" else "buy"
    try:
        exchange.create_order(
            symbol, "STOP_MARKET", sl_side, quantity,
            None, {"stopPrice": float(exchange.price_to_precision(symbol, stop_price)), "closePosition": False}
        )
    except Exception as e:
        print(f"  스톱로스 설정 실패: {e}")

    # 관리 포지션에 등록
    tp_price = current_price + (atr_stop * TRAIL_ACTIVATION_RR) if action == "BUY" else current_price - (atr_stop * TRAIL_ACTIVATION_RR)

    return {
        "side": side,
        "quantity": quantity,
        "leverage": leverage,
        "entry_price": current_price,
        "stop_loss": stop_price,
        "atr_4h": atr_4h,
        "tp_target": round(tp_price, 4),
        "partial_tp_done": False,
        "sl_atr_mult": sl_atr_mult,
    }


# ══════════════════════════════════════
#  리포트
# ══════════════════════════════════════

def build_report(now_str, results, current_positions, trade_log, usdt_total, usdt_free,
                 scan_reasoning, pos_actions, managed_positions):
    total_unrealized = sum(p["unrealized_pnl"] for p in current_positions.values())
    total_exposure = sum(p["notional"] for p in current_positions.values())
    wins = trade_log["wins"]
    losses = trade_log["losses"]
    total_pnl = trade_log["total_pnl"]
    pnl_sign = "+" if total_pnl >= 0 else ""

    lines = [
        f"📅 {now_str} 리포트",
        f"━━━━━━━━━━━━━━━",
        f"💰 총 자산: ${usdt_total:,.0f}",
        f"💵 미실현: ${'+' if total_unrealized >= 0 else ''}{total_unrealized:.1f} | 노출: ${total_exposure:,.0f}",
        f"📊 누적: ${pnl_sign}{total_pnl:.1f} ({wins}승 {losses}패)",
    ]

    # 포지션 관리 액션
    if pos_actions:
        lines.append("")
        lines.extend(pos_actions)

    # 보유 포지션
    lines.append("")
    lines.append("📋 보유 포지션")
    if not current_positions:
        lines.append("  없음")
    else:
        for sym, pos in current_positions.items():
            coin = sym.split("/")[0]
            side_kr = "롱" if pos["side"] == "long" else "숏"
            side_emoji = "🟢" if pos["side"] == "long" else "🔴"
            pnl = pos["unrealized_pnl"]
            pnl_pct = (pnl / pos["notional"] * 100) if pos["notional"] else 0
            mp = managed_positions.get(sym, {})
            sl = mp.get("stop_loss", 0)
            lines.append(f"{side_emoji} {coin} {side_kr} ${pos['notional']:,.0f}")
            lines.append(f"  진입 ${pos['entry_price']:,.2f} → {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% (${'+' if pnl >= 0 else ''}{pnl:.1f})")
            if sl:
                lines.append(f"  손절 ${sl:,.2f}")

    # AI 판단
    lines.append("")
    lines.append("🧠 AI 판단")
    action_map = {"BUY": "🟢매수", "SELL": "🔴매도", "HOLD": "⏸관망"}
    for sym, res in results.items():
        coin = sym.split("/")[0]
        d = res["decision"]
        action_kr = action_map.get(d["action"], d["action"])
        conf = d.get("confidence_score", 0)
        regime = res.get("regime", "")
        lines.append(f"  {coin}: {action_kr} ({conf}%) [{regime}]")

    lines.append("━━━━━━━━━━━━━━━")
    return "\n".join(lines)


# ══════════════════════════════════════
#  메인
# ══════════════════════════════════════

def run_manage_only():
    """매 1시간: 포지션 관리 + 상태 알림"""
    print("=== 포지션 관리 모드 ===")
    exchange = init_exchange()
    managed_positions = load_managed_positions()
    now_str = datetime.now().strftime("%m/%d %H:%M")

    pos_actions = []
    if managed_positions:
        pos_actions = manage_existing_positions(exchange, managed_positions)
        save_managed_positions(managed_positions)

    # 상태 요약 알림
    balance = exchange.fetch_balance()
    usdt_total = float(balance["USDT"]["total"])
    positions = get_exchange_positions(exchange)
    total_unrealized = sum(p["unrealized_pnl"] for p in positions.values())
    wallet_balance = usdt_total - total_unrealized
    trade_log = load_json(TRADE_LOG_FILE, {"wins": 0, "losses": 0, "total_pnl": 0.0})

    lines = [f"⏰ {now_str} 정시 체크",
             f"💰 총 ${usdt_total:,.0f} (지갑 ${wallet_balance:,.0f} + 미실현 ${'+' if total_unrealized >= 0 else ''}{total_unrealized:.1f})",
             f"📈 {trade_log['wins']}승 {trade_log['losses']}패 (${'+' if trade_log['total_pnl'] >= 0 else ''}{trade_log['total_pnl']:.1f})",
             f"📋 포지션: {len(positions)}개"]

    for sym, pos in positions.items():
        coin = sym.split("/")[0]
        side_kr = "롱" if pos["side"] == "long" else "숏"
        pnl = pos["unrealized_pnl"]
        pnl_pct = (pnl / pos["notional"] * 100) if pos["notional"] else 0
        lines.append(f"  {'🟢' if pos['side'] == 'long' else '🔴'} {coin} {side_kr} ${pos['notional']:,.0f} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% / ${'+' if pnl >= 0 else ''}{pnl:.1f})")

    if pos_actions:
        lines.append("")
        lines.extend(pos_actions)

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)


def run_full_analysis():
    """매 4시간: 전체 분석 + 신규 진입 + 리포트"""
    print("=== AI 트레이딩 봇 (전체 분석) ===")
    now_str = datetime.now().strftime("%m/%d %H:%M")

    exchange = init_exchange()
    trade_log = load_json(TRADE_LOG_FILE, {"wins": 0, "losses": 0, "total_pnl": 0.0, "trades": []})
    managed_positions = load_managed_positions()

    # 잔고 & 포지션 조회
    balance = exchange.fetch_balance()
    usdt_total = float(balance["USDT"]["total"])
    usdt_free = float(balance["USDT"]["free"])
    current_positions = get_exchange_positions(exchange)

    print(f"잔고: ${usdt_total:,.2f} | 보유 포지션: {len(current_positions)}개")

    # ── 1단계: 기존 포지션 관리 ──
    print("\n기존 포지션 관리 중...")
    pos_actions = manage_existing_positions(exchange, managed_positions)
    save_managed_positions(managed_positions)
    if pos_actions:
        for a in pos_actions:
            print(a)

    # ── 2단계: 시장 스캔 & 종목 선정 ──
    print("\n시장 스캔 중...")
    selected_symbols, scan_reasoning = scan_market(exchange)

    # 보유 포지션 종목도 분석 대상에 포함 (중복 방지)
    selected_set = set(selected_symbols)
    for sym in current_positions:
        clean = sym.replace(":USDT", "")
        if clean not in selected_set:
            selected_symbols.append(clean)
            selected_set.add(clean)

    # ── 3단계: 종목별 분석 & 주문 ──
    current_positions = get_exchange_positions(exchange)
    results = {}

    for symbol in selected_symbols:
        coin = symbol.split("/")[0]
        print(f"\n[{coin}] 분석 중...")

        tf_data = fetch_multi_tf_indicators(exchange, symbol)
        atr_4h = tf_data["4h"]["atr"].iloc[-1]
        close_4h = tf_data["4h"]["close"].iloc[-1]
        rsi_4h = tf_data["4h"]["rsi"].iloc[-1]
        print(f"  4h 종가: {close_4h:.2f}, RSI: {rsi_4h:.1f}, ATR: {atr_4h:.4f}")

        regime = detect_market_regime(tf_data["4h"])
        print(f"  시장 상태: {regime}")

        funding_rate = get_funding_rate(exchange, symbol)
        print(f"  펀딩비: {funding_rate}%")

        pos_info = current_positions.get(symbol)
        decision = ask_ai(symbol, tf_data, pos_info, regime, funding_rate)
        print(f"  AI: {decision['action']} (확신도: {decision.get('confidence_score', 0)})")
        print(f"  태그: {decision.get('reasoning_tags', [])}")

        trade_result = execute_trade(exchange, symbol, decision, current_positions, usdt_total, atr_4h)

        if trade_result:
            managed_positions[symbol] = trade_result
            save_managed_positions(managed_positions)
            current_positions = get_exchange_positions(exchange)

        results[symbol] = {"decision": decision, "trade": trade_result, "regime": regime}

    # ── 4단계: 리포트 ──
    balance = exchange.fetch_balance()
    usdt_total = float(balance["USDT"]["total"])
    usdt_free = float(balance["USDT"]["free"])
    current_positions = get_exchange_positions(exchange)

    report = build_report(now_str, results, current_positions, trade_log, usdt_total, usdt_free,
                          scan_reasoning, pos_actions, managed_positions)
    print(f"\n{report}")
    send_telegram(report)
    save_json(TRADE_LOG_FILE, trade_log)

    print("\n=== 봇 실행 완료 ===")


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    if mode == "manage":
        run_manage_only()
    else:
        run_full_analysis()
