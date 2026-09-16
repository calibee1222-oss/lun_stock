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
    df = yf.download(symbol, period="1y", interval="1d", progress=False)
    if df.empty:
        return None

    close = df["Close"].squeeze()
    high = df["High"].squeeze()
    low = df["Low"].squeeze()

    for ma in [20, 60, 120, 240]:
        df[f"MA{ma}"] = close.rolling(window=ma).mean()

    delta = close.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df["RSI"] = 100 - (100 / (1 + rs))

    low_9 = low.rolling(9).min()
    high_9 = high.rolling(9).max()
    rsv = (close - low_9) / (high_9 - low_9) * 100
    df["K"] = rsv.ewm(com=2).mean()
    df["D"] = df["K"].ewm(com=2).mean()

    latest = df.iloc[-1]
    curr_p = latest["Close"]

    touched = []
    for ma in [20, 60, 120, 240]:
        val = latest[f"MA{ma}"]
        if pd.notna(val):
            if (
                latest["Low"] <= val <= latest["High"]
                or abs(curr_p - val) / val <= 0.005
            ):
                touched.append(f"{ma}MA({val:.1f})")

    rsi, k, d = latest["RSI"], latest["K"], latest["D"]
    advice = "觀望 / 正常定期定額"
    if rsi <= 30 and k < 30 and k > d:
        advice = "★ 強力分批加碼（超賣＋低檔金叉）"
    elif rsi <= 35 or (k < 25 and k > d):
        advice = "建議小額試單加碼"
    elif rsi >= 75:
        advice = "短線過熱，暫緩追價"

    return {
        "price": curr_p,
        "touched": touched,
        "rsi": rsi,
        "k": k,
        "d": d,
        "advice": advice,
    }


def main():
    report = []
    any_touch = False

    for name, sym in TICKERS.items():
        res = analyze_ticker(sym)
        if not res:
            continue

        status = f"【{name}】收盤/即時: {res['price']:.2f}\n"
        status += (
            f"  - 觸碰均線: {', '.join(res['touched']) if res['touched'] else '無'}\n"
        )
        status += f"  - RSI: {res['rsi']:.1f} | K: {res['k']:.1f} | D: {res['d']:.1f}\n"
        status += f"  - 操作建議: {res['advice']}\n"
        report.append(status)

        if res["touched"]:
            any_touch = True

    # 只要有任何一檔觸碰均線即觸發發信
    if any_touch and report:
        user = os.environ["GMAIL_USER"]
        pwd = os.environ["GMAIL_PASS"]
        to = os.environ["RECEIVER_EMAIL"]

        msg = MIMEMultipart()
        msg["From"] = user
        msg["To"] = to
        msg["Subject"] = "【股市觸碰均線警報】指數投資加碼監控"
        msg.attach(MIMEText("\n".join(report), "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, pwd)
            server.send_message(msg)


if __name__ == "__main__":
    main()
