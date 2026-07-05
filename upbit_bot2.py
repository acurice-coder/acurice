import websocket
import json
import time
from collections import deque
from datetime import datetime, timedelta
import requests
import threading

# ===============================
# 텔레그램 설정
# ===============================
TELEGRAM_TOKEN = '8468033376:AAGEdBpXQR7uWr_VVmdfEulEKzIYx6OJiuA'
CHAT_ID = '8176087189'

def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg}, timeout=5)
    except:
        pass

def status(msg):
    print("[STATUS]", msg)
    send(f"📡 {msg}")

# ===============================
# 설정
# ===============================
RSI_PERIOD = 14
RSI_LOW = 39

COOLDOWN_MINUTES = 30
CANDLE_WINDOW = 120
UNIQUE_WINDOW = 60 * 60

# ===============================
# 상태
# ===============================
candles = {}
current_min = {}
change_rates = {}
unique_prices = {}
last_alert_time = {}

last_data_time = time.time()

# ===============================
# 마켓
# ===============================
def get_krw_markets():
    try:
        data = requests.get(
            "https://api.upbit.com/v1/market/all",
            timeout=5
        ).json()

        return {
            x['market']: x['korean_name']
            for x in data
            if x['market'].startswith("KRW-")
        }
    except:
        return {}

MARKETS = get_krw_markets()

# ===============================
# RSI (Wilder)
# ===============================
def calc_rsi(closes):
    if len(closes) < RSI_PERIOD + 1:
        return None

    gains = []
    losses = []

    for i in range(1, RSI_PERIOD + 1):
        diff = closes[i] - closes[i - 1]

        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / RSI_PERIOD
    avg_loss = sum(losses) / RSI_PERIOD

    for i in range(RSI_PERIOD + 1, len(closes)):
        diff = closes[i] - closes[i - 1]

        gain = max(diff, 0)
        loss = max(-diff, 0)

        avg_gain = (
            (avg_gain * (RSI_PERIOD - 1)) + gain
        ) / RSI_PERIOD

        avg_loss = (
            (avg_loss * (RSI_PERIOD - 1)) + loss
        ) / RSI_PERIOD

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss

    return round(
        100 - (100 / (1 + rs)),
        2
    )

# ===============================
# 봉 생성률 (120분 기준)
# ===============================
def calc_candle_rate(market, now):
    start = now - timedelta(minutes=CANDLE_WINDOW)

    data = candles.get(market, [])

    count = sum(
        1 for t, _ in data
        if t >= start
    )

    return round(
        (count / CANDLE_WINDOW) * 100,
        2
    )

# ===============================
# 유니크 가격
# ===============================
def get_unique_count(market):
    now = time.time()

    dq = unique_prices.get(
        market,
        deque()
    )

    values = [
        p for t, p in dq
        if now - t <= UNIQUE_WINDOW
    ]

    return len(set(values))

# ===============================
# 등락률 순위
# ===============================
def get_rank(market):
    sorted_items = sorted(
        change_rates.items(),
        key=lambda x: x[1],
        reverse=True
    )

    for i, (m, _) in enumerate(
        sorted_items,
        1
    ):
        if m == market:
            return i

    return None

# ===============================
# 프리로드
# ===============================
def preload():
    status("프리로드 시작")

    for market in MARKETS:
        try:
            data = requests.get(
                "https://api.upbit.com/v1/candles/minutes/1",
                params={
                    "market": market,
                    "count": 200
                },
                timeout=5
            ).json()

            dq = deque(maxlen=500)

            for item in reversed(data):
                dt = datetime.strptime(
                    item['candle_date_time_kst'],
                    "%Y-%m-%dT%H:%M:%S"
                )

                dq.append(
                    (
                        dt,
                        item['trade_price']
                    )
                )

            if dq:
                candles[market] = dq

                current_min[market] = (
                    dq[-1][0]
                    .strftime("%Y-%m-%d %H:%M")
                )

        except:
            continue

    status("프리로드 완료")

# ===============================
# WebSocket
# ===============================
def on_message(ws, message):
    global last_data_time

    try:
        last_data_time = time.time()

        data = json.loads(message)

        market = data.get('code')
        price = data.get('trade_price')

        if not market or price is None:
            return

        if "signed_change_rate" in data:
            change_rates[market] = (
                data["signed_change_rate"] * 100
            )

        unique_prices.setdefault(
            market,
            deque(maxlen=2000)
        ).append(
            (
                time.time(),
                price
            )
        )

        ts = datetime.fromtimestamp(
            data['trade_timestamp'] / 1000
        )

        minute_dt = ts.replace(
            second=0,
            microsecond=0
        )

        minute_str = minute_dt.strftime(
            "%Y-%m-%d %H:%M"
        )

        if market not in candles:
            return

        # 같은 분이면 종가 갱신
        if minute_str == current_min.get(market):
            candles[market][-1] = (
                minute_dt,
                price
            )
            return

        # 새 분 생성
        current_min[market] = minute_str

        candles[market].append(
            (
                minute_dt,
                price
            )
        )

        closes = [
            c[1]
            for c in candles[market]
        ]

        rsi = calc_rsi(closes)

        if rsi is None:
            return

        # ===============================
        # RSI 조건
        # ===============================
        if rsi >= RSI_LOW:
            return

        # ===============================
        # 추가 필터
        # ===============================
        candle_rate = calc_candle_rate(
            market,
            minute_dt
        )

        if candle_rate < 80:
            return

        unique = get_unique_count(market)

        if unique < 5:
            return

        # ===============================
        # 쿨타임 체크
        # ===============================
        now = datetime.now()

        last = last_alert_time.get(
            market
        )

        if (
            last and
            (now - last)
            < timedelta(
                minutes=COOLDOWN_MINUTES
            )
        ):
            return

        # ===============================
        # 알림
        # ===============================
        name = MARKETS.get(
            market,
            market
        )

        rank = get_rank(market)

        rate = change_rates.get(
            market,
            0
        )

        last_alert_time[market] = now

        send(
            f"🚨 RSI39미만 감지 🚨\n"
            f"{name} ({market})\n"
            f"현재가격: {price:,}\n"
            f"등락률: {rate:.2f}% ({rank}위)\n"
            f"봉생성률: {candle_rate}%\n"
            f"유니크가격수: {unique}\n"
            f"RSI: {rsi}"
        )

    except Exception:
        pass

def on_open(ws):
    status("🟢 연결됨")

    ws.send(
        json.dumps([
            {"ticket": "rsi39"},
            {
                "type": "ticker",
                "codes": list(MARKETS.keys())
            }
        ])
    )

# ===============================
# heartbeat
# ===============================
def heartbeat():
    global last_data_time

    while True:
        time.sleep(30)

        if (
            time.time()
            - last_data_time
            > 60
        ):
            status(
                "⚠️ 데이터 끊김 감지"
            )

# ===============================
# 실행
# ===============================
def run():
    threading.Thread(
        target=heartbeat,
        daemon=True
    ).start()

    while True:
        try:
            ws = websocket.WebSocketApp(
                "wss://api.upbit.com/websocket/v1",
                on_message=on_message,
                on_open=on_open
            )

            ws.run_forever()

        except Exception as e:
            status(
                f"재연결: {e}"
            )

            time.sleep(5)

# ===============================
# MAIN
# ===============================
if __name__ == "__main__":
    status("🚀 시작")

    preload()

    run()
