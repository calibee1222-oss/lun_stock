#!/usr/bin/env python3
"""
四大指數加碼監控：抓取 TAIEX / QQQ / SPY / 半導體ETF 的每日資料，
計算 3 日 / 5 日累計漲跌幅與 KD 值，在台股/美股開盤收盤時間點
透過 ntfy.sh 推播通知到手機或電腦。

由 GitHub Actions 排程觸發，完全免費、不需要自己的電腦或手機開機。
"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# ---------- 設定（可用 GitHub Actions 的 env / secrets 覆蓋） ----------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
AV_KEY = os.environ.get("ALPHA_VANTAGE_KEY", "").strip()

TH3 = float(os.environ.get("TH3", "3"))
TH5 = float(os.environ.get("TH5", "5"))
KD_BUY = float(os.environ.get("KD_BUY", "30"))
KD_SELL = float(os.environ.get("KD_SELL", "70"))
KD_OVERSOLD = float(os.environ.get("KD_OVERSOLD", "20"))
KD_OVERBOUGHT = float(os.environ.get("KD_OVERBOUGHT", "80"))

# 判斷「現在算不算在監控時間窗口內」的容忍分鐘數。
# GitHub Actions 的排程觸發時間本來就不保證分秒不差，甚至常常晚個幾分鐘，
# 所以用「時間窗口」而不是「精準比對某一分鐘」來判斷。
TOLERANCE_MIN = int(os.environ.get("TOLERANCE_MIN", "12"))

TICKERS = [
    {"id": "taiex", "symbol": "^TWII", "label": "台股加權指數 TAIEX"},
    {"id": "qqq", "symbol": "QQQ", "label": "QQQ"},
    {"id": "spy", "symbol": "SPY", "label": "SPY"},
    {"id": "semi", "symbol": os.environ.get("SEMI_SYMBOL", "SOXX"), "label": "半導體ETF"},
]

# 美股有日光節約時間，所以開盤/收盤各排兩個 UTC 時間點在 workflow 裡觸發，
# 這裡用「當下的實際美東時間」再判斷一次，兩次觸發只會有一次真的落在窗口內。
MARKET_SLOTS = [
    {"key": "tw_open", "tz": "Asia/Taipei", "hour": 9, "minute": 0, "label": "🇹🇼 台股開盤"},
    {"key": "tw_close", "tz": "Asia/Taipei", "hour": 13, "minute": 30, "label": "🇹🇼 台股收盤"},
    {"key": "us_open", "tz": "America/New_York", "hour": 9, "minute": 30, "label": "🇺🇸 美股開盤"},
    {"key": "us_close", "tz": "America/New_York", "hour": 16, "minute": 0, "label": "🇺🇸 美股收盤"},
]


def fetch_yahoo(symbol):
    """伺服器端呼叫 Yahoo Finance，沒有瀏覽器那種 CORS 限制，通常比較穩定。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    result = data["chart"]["result"][0]
    ts = result["timestamp"]
    quote = result["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        c = quote["close"][i]
        if c is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open": quote["open"][i] or c,
            "high": quote["high"][i] or c,
            "low": quote["low"][i] or c,
            "close": c,
        })
    return rows


def fetch_alpha_vantage(symbol):
    if not AV_KEY:
        return None
    url = (
        "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
        f"&symbol={symbol}&outputsize=full&apikey={AV_KEY}"
    )
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    series = data.get("Time Series (Daily)")
    if not series:
        return None
    rows = []
    for date, v in series.items():
        rows.append({
            "date": date,
            "open": float(v["1. open"]),
            "high": float(v["2. high"]),
            "low": float(v["3. low"]),
            "close": float(v["4. close"]),
        })
    rows.sort(key=lambda r: r["date"])
    return rows


def get_history(ticker):
    """個股/ETF 先試 Alpha Vantage，指數（沒有 AV 資料）或 AV 失敗時改用 Yahoo。"""
    rows = None
    if ticker["id"] != "taiex":
        try:
            rows = fetch_alpha_vantage(ticker["symbol"])
        except Exception as e:
            print(f"[{ticker['symbol']}] Alpha Vantage 失敗：{e}")
            rows = None
    if not rows:
        try:
            rows = fetch_yahoo(ticker["symbol"])
        except Exception as e:
            print(f"[{ticker['symbol']}] Yahoo Finance 失敗：{e}")
            rows = None
    if rows:
        rows.sort(key=lambda r: r["date"])
    return rows or []


def compute_kd(rows, n=9):
    K, D = [], []
    prev_k, prev_d = 50.0, 50.0
    for i in range(len(rows)):
        if i + 1 < n:
            K.append(None)
            D.append(None)
            continue
        window = rows[i - n + 1:i + 1]
        hh = max(r["high"] for r in window)
        ll = min(r["low"] for r in window)
        c = rows[i]["close"]
        rsv = 50.0 if hh == ll else (c - ll) / (hh - ll) * 100
        k = prev_k * 2 / 3 + rsv * 1 / 3
        d = prev_d * 2 / 3 + k * 1 / 3
        K.append(k)
        D.append(d)
        prev_k, prev_d = k, d
    return K, D


def pct_change(closes, n):
    if len(closes) < n + 1:
        return None
    base = closes[-1 - n]
    if not base:
        return None
    return (closes[-1] / base - 1) * 100


def tier_for(chg3, chg5):
    b3 = chg3 is not None and chg3 <= -TH3
    b5 = chg5 is not None and chg5 <= -TH5
    if b5:
        return 2
    if b3:
        return 1
    return 0


def kd_suggestion(K, D):
    idxs = [i for i in range(len(K)) if K[i] is not None]
    if len(idxs) < 2:
        return "資料不足，無法判斷 KD"
    i0, i1 = idxs[-2], idxs[-1]
    k0, d0, k1, d1 = K[i0], D[i0], K[i1], D[i1]
    golden = k0 <= d0 and k1 > d1
    dead = k0 >= d0 and k1 < d1
    if golden and k1 < KD_BUY:
        return f"🟢 KD低檔黃金交叉(K{k1:.1f}/D{d1:.1f})，進場訊號"
    if dead and k1 > KD_SELL:
        return f"🔴 KD高檔死亡交叉(K{k1:.1f}/D{d1:.1f})，減碼風險"
    if golden:
        return f"🙂 黃金交叉但非低檔(K{k1:.1f})，訊號較弱"
    if dead:
        return f"🙂 死亡交叉但非高檔(K{k1:.1f})，訊號較弱"
    if k1 < KD_OVERSOLD:
        return f"👀 超賣區(K={k1:.1f})"
    if k1 > KD_OVERBOUGHT:
        return f"⚠️ 超買區(K={k1:.1f})"
    return f"🙂 中性(K={k1:.1f}/D={d1:.1f})"


def fmt_pct(v):
    if v is None:
        return "—"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%"


def build_summary_line(ticker, rows):
    closes = [r["close"] for r in rows]
    chg3 = pct_change(closes, 3)
    chg5 = pct_change(closes, 5)
    tier = tier_for(chg3, chg5)
    icon = "🔴" if tier == 2 else "🟡" if tier == 1 else "🟢"
    kd_text = ""
    if len(rows) >= 10:
        K, D = compute_kd(rows, 9)
        kd_text = " ｜ " + kd_suggestion(K, D)
    return f"{icon} {ticker['label']}：3日{fmt_pct(chg3)}／5日{fmt_pct(chg5)}{kd_text}"


def send_ntfy(title, body):
    if not NTFY_TOPIC:
        print("⚠️ 尚未設定 NTFY_TOPIC，只印出結果，不會推播：")
        print(title)
        print(body)
        return
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": "default",
                "Tags": "chart_with_upwards_trend",
            },
            timeout=10,
        )
        print("已推播到 ntfy：", title)
    except Exception as e:
        print(f"ntfy 推播失敗：{e}")


def current_slot():
    now = datetime.now(timezone.utc)
    for slot in MARKET_SLOTS:
        local = now.astimezone(ZoneInfo(slot["tz"]))
        if local.weekday() >= 5:  # 週六=5, 週日=6，跳過（國定假日休市未處理）
            continue
        target_minutes = slot["hour"] * 60 + slot["minute"]
        now_minutes = local.hour * 60 + local.minute
        if abs(now_minutes - target_minutes) <= TOLERANCE_MIN:
            return slot
    return None


def main():
    slot = current_slot()
    force = os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")
    if not slot and not force:
        print("目前不在任何監控時間窗口內，結束（若要手動測試請用 workflow_dispatch 並開啟 FORCE_RUN）。")
        return
    label = slot["label"] if slot else "🧪 手動測試"

    lines = []
    for ticker in TICKERS:
        rows = get_history(ticker)
        if len(rows) < 10:
            lines.append(f"⚪ {ticker['label']}：資料不足（僅取得 {len(rows)} 筆）")
            continue
        lines.append(build_summary_line(ticker, rows))

    body = "\n".join(lines)
    print(body)
    send_ntfy(f"{label} ・ 監控結果", body)


if __name__ == "__main__":
    main()
