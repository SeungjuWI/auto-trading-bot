import os
import ccxt
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY")

# 바이낸스 선물 데모 트레이딩 연결
exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_SECRET_KEY,
    "options": {"defaultType": "future"},
})
exchange.enable_demo_trading(True)

# USDT 선물 잔고 조회
balance = exchange.fetch_balance()
usdt_balance = balance["USDT"]["total"]
print(f"테스트넷 USDT 잔고: {usdt_balance}")

# 텔레그램 메시지 전송
message = f"🤖 자동 매매 봇 연동 테스트 완료!\n💰 현재 테스트넷 잔고: {usdt_balance} USDT"
url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
response = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message})

if response.status_code == 200:
    print("텔레그램 메시지 전송 성공!")
else:
    print(f"텔레그램 메시지 전송 실패: {response.text}")
