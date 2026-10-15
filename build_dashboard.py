"""
指數 ETF 長期投資儀表板 — 資料產生器
執行後輸出 docs/data.json，供 docs/index.html 顯示。

燈號意義（全部統一）：
  🟢 green  = 建議加碼
  🟡 yellow = 正常定期定額
  🔴 red    = 偏貴／過熱，不加碼（定期定額照扣，不是賣出訊號）
"""
import json
import math
import re
import datetime as dt
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

TW_TZ = dt.timezone(dt.timedelta(hours=8))
OUT = Path(__file__).parent / "docs" / "data.json"
UA = {"User-Agent": "Mozilla/5.0 (StockDashboard)"}

# ─────────────────────────── 設定區（門檻都可以自己調） ───────────────────────────
INDICES = [
    # key, 顯示名稱, yfinance 代號, 市場
    ("taiex", "台股大盤", "^TWII", "TW"),
    ("0050", "0050 ETF", "0050.TW", "TW"),
    ("sox", "費半 SOX", "^SOX", "US"),
    ("ndx", "那斯達克100", "^NDX", "US"),
    ("spy", "標普 SPY", "SPY", "US"),
]

T = {
    # 距離近 3 年高點回撤（%）：越深越該加碼
    "drawdown": {"green": -10, "big": -20, "huge": -30, "red": -5},
    # 年線(240MA)乖離（%）：低於年線綠燈，過熱紅燈
    "bias240": {"green": 0, "red": 15},
    # 個別指數覆寫：費半波動約大盤兩倍，年線乖離放寬到 30% 才算過熱
    "bias240_by_index": {"sox": {"green": 0, "red": 30}},
    "rsi": {"green": 30, "red": 70},
    "k": {"green": 20, "red": 80},
    "vix": {"green": 25, "red": 15},             # ≥25 恐慌=綠；<15 過度樂觀=紅
    "us_pe": {"green": 18, "red": 25},           # S&P 500 本益比
    "tw_pe": {"green": 13, "red": 18},           # 上市公司本益比中位數
    "us10y": {"green": 3.5, "red": 4.5},         # 美債 10 年殖利率 %
    "usdtwd": {"green": -1.0, "red": 1.0},       # 相對一年均價 %：台幣強=綠
    # 綜合判斷：總分 = 綠燈數 − 紅燈數
    "verdict": {"green": 3, "red": -3},
}

# 景氣燈號：自動抓失敗時用這個（國發會每月 27 日左右公布，手動更新即可）
MANUAL_NDC = {"month": "2026-08", "score": 41}  # 2026/9/29 公布：紅燈 41 分

# 各市場要參考的總經指標
MACRO_FOR = {"TW": ["ndc", "tw_pe", "vix"], "US": ["us_pe", "us10y", "vix", "usdtwd"]}


# ─────────────────────────── 工具函式 ───────────────────────────
def light(value, green, red, higher_is_green=False):
    """回傳 green / yellow / red。預設數值越低越該加碼。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "na"
    if higher_is_green:
        return "green" if value >= green else "red" if value < red else "yellow"
    return "green" if value <= green else "red" if value > red else "yellow"


def r(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def history(ticker, period="3y"):
    df = yf.download(ticker, period=period, interval="1d", progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    # 修正 0050 = nan：盤中當天那一列常是空值，丟掉只留有效收盤
    return df.dropna(subset=["Close"])


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def kd(df, n=9):
    low, high = df["Low"].rolling(n).min(), df["High"].rolling(n).max()
    rsv = ((df["Close"] - low) / (high - low) * 100).fillna(50)
    k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    d = k.ewm(alpha=1 / 3, adjust=False).mean()
    return k, d


# ─────────────────────────── 個別指數 ───────────────────────────
def analyze_index(key, name, ticker, market):
    df = history(ticker).dropna(subset=["Close"])
    if len(df) < 30:
        raise ValueError("資料筆數不足")
    c = df["Close"]
    price = float(c.iloc[-1])
    peak = float(c.max())
    dd = (price / peak - 1) * 100
    ma240 = c.rolling(240).mean().iloc[-1]
    bias = (price / ma240 - 1) * 100 if not math.isnan(ma240) else float("nan")
    rsi_v = float(rsi(c).iloc[-1])
    k, d = kd(df)

    bt = T["bias240_by_index"].get(key, T["bias240"])
    t = T["drawdown"]
    if dd <= t["huge"]:
        dd_note = "大幅加碼區"
    elif dd <= t["big"]:
        dd_note = "中度加碼區"
    elif dd <= t["green"]:
        dd_note = "小幅加碼區"
    elif dd > t["red"]:
        dd_note = "接近高點"
    else:
        dd_note = "小回檔"

    items = [
        {"id": "drawdown", "label": "距高點回撤", "value": r(dd, 1), "unit": "%",
         "light": light(dd, t["green"], t["red"]), "note": dd_note,
         "detail": f"近 3 年高點 {peak:,.2f}"},
        {"id": "bias240", "label": "年線乖離", "value": r(bias, 1), "unit": "%",
         "light": light(bias, bt["green"], bt["red"]),
         "detail": f"240MA {ma240:,.2f}" if not math.isnan(ma240) else "資料不足"},
        {"id": "rsi", "label": "RSI(14)", "value": r(rsi_v, 1), "unit": "",
         "light": light(rsi_v, T["rsi"]["green"], T["rsi"]["red"]), "detail": "短線參考"},
        {"id": "kd", "label": "KD", "value": r(k.iloc[-1], 1), "unit": "",
         "light": light(float(k.iloc[-1]), T["k"]["green"], T["k"]["red"]),
         "detail": f"K {k.iloc[-1]:.1f} / D {d.iloc[-1]:.1f}・短線參考"},
    ]
    spark = [r(v) for v in c.iloc[-120:].tolist()]
    return {"key": key, "name": name, "ticker": ticker, "market": market,
            "price": r(price), "date": str(c.index[-1].date()),
            "change": r((price / float(c.iloc[-2]) - 1) * 100, 2),
            "items": items, "spark": spark}


# ─────────────────────────── 總經指標 ───────────────────────────
def macro_vix():
    c = history("^VIX", "1y")["Close"]
    v = float(c.iloc[-1])
    return {"id": "vix", "label": "VIX 恐慌指數", "value": r(v, 1), "unit": "",
            "light": light(v, T["vix"]["green"], T["vix"]["red"], higher_is_green=True),
            "detail": "≥25 恐慌加碼；<15 市場過度樂觀", "source": "Yahoo Finance ^VIX"}


def macro_us10y():
    c = history("^TNX", "1y")["Close"]
    v = float(c.iloc[-1])  # ^TNX 已是百分比數值
    return {"id": "us10y", "label": "美債 10 年殖利率", "value": r(v, 2), "unit": "%",
            "light": light(v, T["us10y"]["green"], T["us10y"]["red"]),
            "detail": "利率越高，股票估值壓力越大", "source": "Yahoo Finance ^TNX"}


def macro_usdtwd():
    c = history("TWD=X", "1y")["Close"]
    v, avg = float(c.iloc[-1]), float(c.mean())
    diff = (v / avg - 1) * 100
    return {"id": "usdtwd", "label": "美元/台幣", "value": r(v, 3), "unit": "",
            "light": light(diff, T["usdtwd"]["green"], T["usdtwd"]["red"]),
            "detail": f"一年均價 {avg:.2f}（{diff:+.1f}%）・台幣強時買美股較划算",
            "source": "Yahoo Finance TWD=X"}


def macro_us_pe():
    v, src = None, ""
    try:
        html = requests.get("https://www.multpl.com/s-p-500-pe-ratio", headers=UA, timeout=20).text
        m = re.search(r'id="current".*?([\d]+\.[\d]+)', html, re.S)
        if m:
            v, src = float(m.group(1)), "multpl.com（S&P 500 本益比）"
    except Exception:
        pass
    if v is None:  # 備援：SPY 自身本益比
        try:
            v, src = float(yf.Ticker("SPY").info.get("trailingPE")), "Yahoo Finance SPY trailingPE（備援）"
        except Exception:
            src = "抓取失敗"
    return {"id": "us_pe", "label": "S&P 500 本益比", "value": r(v, 1), "unit": "倍",
            "light": light(v, T["us_pe"]["green"], T["us_pe"]["red"]),
            "detail": "<18 便宜；>25 偏貴", "source": src}


def macro_tw_pe():
    """上市公司本益比中位數（證交所 OpenAPI 每日資料）。大盤整體本益比沒有穩定 API，用中位數當代理。"""
    v, src = None, "抓取失敗"
    try:
        rows = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
                            headers=UA, timeout=30).json()
        pes = []
        for row in rows:
            try:
                pe = float(row.get("PEratio") or row.get("本益比") or 0)
                if 0 < pe < 200:
                    pes.append(pe)
            except ValueError:
                continue
        if pes:
            v, src = float(pd.Series(pes).median()), f"證交所 BWIBBU_ALL（{len(pes)} 檔中位數）"
    except Exception:
        pass
    return {"id": "tw_pe", "label": "台股本益比（中位數）", "value": r(v, 1), "unit": "倍",
            "light": light(v, T["tw_pe"]["green"], T["tw_pe"]["red"]),
            "detail": "<13 便宜；>18 偏貴", "source": src}


NDC_LIGHTS = [(16, "藍燈", "green"), (22, "黃藍燈", "green"), (31, "綠燈", "yellow"),
              (37, "黃紅燈", "red"), (45, "紅燈", "red")]


def macro_ndc():
    score, month, src = None, "", ""
    try:
        # 國發會景氣指標查詢系統（第一次跑請確認此端點仍可用；失敗會改用 MANUAL_NDC）
        j = requests.get("https://index.ndc.gov.tw/n/json/data/eco/indicators",
                         headers=UA, timeout=20).json()
        line = next(x for x in j["line"].values() if "對策信號" in x.get("name", ""))
        last = [p for p in line["data"] if p.get("y") is not None][-1]
        score, month, src = int(last["y"]), str(last.get("x", "")), "國發會景氣指標查詢系統"
    except Exception:
        if MANUAL_NDC["score"] is not None:
            score, month, src = MANUAL_NDC["score"], MANUAL_NDC["month"], "手動輸入（MANUAL_NDC）"
        else:
            src = "抓取失敗，請在 MANUAL_NDC 手動填入"
    name, lt = "—", "na"
    if score is not None:
        for top, n, l in NDC_LIGHTS:
            if score <= top:
                name, lt = n, l
                break
    return {"id": "ndc", "label": "景氣對策信號", "value": score, "unit": "分",
            "light": lt, "note": name,
            "detail": f"{month} {name}・藍/黃藍燈加碼，紅燈不追", "source": src}


def safe(fn, fallback_id, label):
    try:
        return fn()
    except Exception as e:
        return {"id": fallback_id, "label": label, "value": None, "unit": "",
                "light": "na", "detail": f"抓取失敗：{type(e).__name__}", "source": ""}


# ─────────────────────────── 綜合判斷 ───────────────────────────
def verdict(lights):
    score = lights.count("green") - lights.count("red")
    if score >= T["verdict"]["green"]:
        return score, "green", "建議加碼"
    if score <= T["verdict"]["red"]:
        return score, "red", "暫停加碼・定期定額照扣"
    return score, "yellow", "正常定期定額"


def build():
    macro = {m["id"]: m for m in [
        safe(macro_ndc, "ndc", "景氣對策信號"),
        safe(macro_tw_pe, "tw_pe", "台股本益比"),
        safe(macro_us_pe, "us_pe", "S&P 500 本益比"),
        safe(macro_vix, "vix", "VIX 恐慌指數"),
        safe(macro_us10y, "us10y", "美債 10 年殖利率"),
        safe(macro_usdtwd, "usdtwd", "美元/台幣"),
    ]}
    indices = []
    for key, name, ticker, market in INDICES:
        try:
            idx = analyze_index(key, name, ticker, market)
        except Exception as e:
            indices.append({"key": key, "name": name, "ticker": ticker, "market": market,
                            "error": f"{type(e).__name__}: {e}"})
            continue
        lights = [i["light"] for i in idx["items"]] + [macro[m]["light"] for m in MACRO_FOR[market]]
        idx["macro"] = MACRO_FOR[market]
        idx["score"], idx["light"], idx["verdict"] = verdict(lights)
        indices.append(idx)

    data = {"updated": dt.datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M"),
            "thresholds": T, "macro": macro, "indices": indices}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


if __name__ == "__main__":
    d = build()
    for i in d["indices"]:
        print(i["name"], i.get("price"), i.get("verdict"), i.get("score"), i.get("error", ""))
