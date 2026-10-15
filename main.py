import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pandas as pd
import yfinance as yf

import build_dashboard

TICKERS = {
    "台股大盤": "^TWII",
    "0050 ETF": "0050.TW",
    "費半 SOX": "^SOX",
    "那斯達克100": "^NDX",
    "標普 SPY": "SPY",
}


def analyze_ticker(symbol):
    try:
        # 下載過去一年日 K 線資料
        df = yf.download(
            symbol, period="1y", interval="1d", progress=False, auto_adjust=True
        )
        if df.empty or len(df) < 60:
            return None

        # 扁平化多層欄位 (避免新版 yfinance MultiIndex 問題)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # 修正 0050 = nan：盤中當天那一列常是空值，丟掉只留有效收盤
        df = df.dropna(subset=["Close"])

        # 確保為 1D 浮點數 Series
        close = df["Close"].squeeze().astype(float)
        high = df["High"].squeeze().astype(float)
        low = df["Low"].squeeze().astype(float)

        # 1. 計算均線 (20, 60, 120, 240)
        ma_dict = {}
        for ma in [20, 60, 120, 240]:
            ma_series = close.rolling(window=ma).mean()
            val = ma_series.iloc[-1]
            ma_dict[ma] = float(val) if pd.notna(val) else None

        # 2. 計算 RSI (14)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / (loss + 1e-9)
        rsi_series = 100 - (100 / (1 + rs))
        rsi_val = float(rsi_series.iloc[-1])

        # 3. 計算 KD (9, 3, 3)
        low_9 = low.rolling(9).min()
        high_9 = high.rolling(9).max()
        rsv = ((close - low_9) / (high_9 - low_9 + 1e-9)) * 100
        k_series = rsv.ewm(com=2).mean()
        d_series = k_series.ewm(com=2).mean()
        k_val = float(k_series.iloc[-1])
        d_val = float(d_series.iloc[-1])

        curr_p = float(close.iloc[-1])
        curr_low = float(low.iloc[-1])
        curr_high = float(high.iloc[-1])

        # 判斷是否觸碰均線 (高低價涵蓋或收盤價誤差在 0.5% 內)
        touched = []
        for ma, ma_val in ma_dict.items():
            if ma_val is not None:
                if (
                    curr_low <= ma_val <= curr_high
                    or abs(curr_p - ma_val) / ma_val <= 0.005
                ):
                    touched.append(f"{ma}MA ({ma_val:.2f})")

        # 建議評估
        advice = "觀望 / 正常定期定額"
        if rsi_val <= 30 and k_val < 30 and k_val > d_val:
            advice = "★ 強力分批加碼（超賣＋低檔金叉）"
        elif rsi_val <= 35 or (k_val < 25 and k_val > d_val):
            advice = "建議小額分批試單加碼"
        elif rsi_val >= 75:
            advice = "短線過熱，暫緩追價"

        return {
            "price": curr_p,
            "touched": touched,
            "rsi": rsi_val,
            "k": k_val,
            "d": d_val,
            "advice": advice,
        }
    except Exception as e:
        print(f"處理 {symbol} 時發生錯誤: {e}")
        return None


DASHBOARD_URL = "https://calibee1222-oss.github.io/lun_stock/"
EMOJI = {"green": "🟢", "yellow": "🟡", "red": "🔴", "na": "⚪"}


def main():
    # 1. 產生儀表板資料（docs/data.json），同時拿來寫信
    try:
        dash = build_dashboard.build()
    except Exception as e:
        print(f"儀表板資料產生失敗: {e}")
        dash = {"indices": [], "macro": {}}
    by_ticker = {i["ticker"]: i for i in dash["indices"]}

    report = []
    any_touch = False
    greens = []

    for name, sym in TICKERS.items():
        res = analyze_ticker(sym)
        idx = by_ticker.get(sym, {})
        if not res and not idx.get("items"):
            report.append(f"【{name}】資料獲取失敗\n")
            continue

        if idx.get("light"):
            status = f"{EMOJI[idx['light']]}【{name}】{idx['verdict']}（總分 {idx['score']:+d}）\n"
            if idx["light"] == "green":
                greens.append(name)
        else:
            status = f"⚪【{name}】\n"

        if res:
            status += f"  - 價格: {res['price']:.2f}\n"
        for it in idx.get("items", []):
            if it["id"] in ("drawdown", "bias240"):
                v = "—" if it["value"] is None else f"{it['value']:+.1f}%"
                status += f"  - {EMOJI[it['light']]} {it['label']}: {v}\n"
        if res:
            touch_str = (
                "⚠️ 觸碰: " + ", ".join(res["touched"]) if res["touched"] else "未觸碰"
            )
            if res["touched"]:
                any_touch = True
            status += f"  - 均線狀態: {touch_str}\n"
            status += (
                f"  - RSI: {res['rsi']:.1f} | K: {res['k']:.1f} | D: {res['d']:.1f}（短線參考）\n"
            )
        report.append(status)

    macro_lines = []
    for m in dash.get("macro", {}).values():
        v = "—" if m["value"] is None else f"{m['value']}{m.get('unit', '')}"
        note = f" {m['note']}" if m.get("note") else ""
        macro_lines.append(f"  {EMOJI[m['light']]} {m['label']}: {v}{note}")
    if macro_lines:
        report.append("【市場溫度】\n" + "\n".join(macro_lines) + "\n")

    report.append(f"完整儀表板：{DASHBOARD_URL}\n🟢 建議加碼　🟡 正常定期定額　🔴 不加碼（定期定額照扣）")
    content = "\n".join(report)
    print(content)

    # 取得環境變數發信
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_PASS")
    to = os.environ.get("RECEIVER_EMAIL")

    if not (user and pwd and to):
        print("缺少 Gmail 機密環境變數，略過發信。")
        return

    if greens:
        subject = f"【🟢加碼訊號】{'、'.join(greens)}"
    elif any_touch:
        subject = "【股市均線警報⚠️】觸碰重要均線提醒！"
    else:
        subject = "【股市例行報表】指數投資監控日報"

    msg = MIMEMultipart()
    msg["From"] = user
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(content, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, pwd)
        server.send_message(msg)
    print("郵件已成功寄出！")


if __name__ == "__main__":
    main()
