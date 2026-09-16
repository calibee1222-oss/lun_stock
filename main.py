import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import pandas as pd
import yfinance as yf

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


def main():
    report = []
    any_touch = False

    for name, sym in TICKERS.items():
        res = analyze_ticker(sym)
        if not res:
            report.append(f"【{name}】資料獲取失敗\n")
            continue

        touch_str = (
            "⚠️ 觸碰: " + ", ".join(res["touched"]) if res["touched"] else "未觸碰"
        )
        if res["touched"]:
            any_touch = True

        status = f"【{name}】價格: {res['price']:.2f}\n"
        status += f"  - 均線狀態: {touch_str}\n"
        status += (
            f"  - RSI: {res['rsi']:.1f} | K: {res['k']:.1f} | D: {res['d']:.1f}\n"
        )
        status += f"  - 操作建議: {res['advice']}\n"
        report.append(status)

    content = "\n".join(report)
    print(content)

    # 取得環境變數發信
    user = os.environ.get("GMAIL_USER")
    pwd = os.environ.get("GMAIL_PASS")
    to = os.environ.get("RECEIVER_EMAIL")

    if not (user and pwd and to):
        print("缺少 Gmail 機密環境變數，略過發信。")
        return

    subject = (
        "【股市均線警報⚠️】觸碰重要均線提醒！"
        if any_touch
        else "【股市例行報表】指數投資監控日報"
    )

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
