"""
ETF 系統全欄位診斷腳本 v4
用法：python diagnose.py [--tw 代碼] [--us 代碼]
預設測試：台股 0050、00878，美股 VOO、SCHD

修正重點（v4）：
  ✦ TWSE 請求不帶 Accept-Encoding（解決 gzip 亂碼 / JSON 解析失敗）
  ✦ Yahoo 429 自動重試最多 3 次（10s → 20s → 40s）
  ✦ 每次 Yahoo 請求間固定 sleep 5 秒
  ✦ asset_size 失敗時使用靜態對照表備援（與 main.py 一致）
  ✦ 完整顯示 TWSE 原始回傳以供排查
  ✦ 台股 / 美股 分流診斷，欄位對應 main.py 實際使用

測試欄位：
  current_price / price_change / price_change_percent
  volume / day_high / day_low
  dividend_yield / payout_freq
  annual_return_1y / 3y / 5y
  asset_size / pe_ratio / expense_ratio
  fifty_two_week_high / fifty_two_week_low
"""

import sys, time, random, datetime

try:
    import requests
except ImportError:
    sys.exit("❌  請先安裝 requests：pip install requests")

# ──────────────────────────────────────────
# 命令列參數
# ──────────────────────────────────────────
TW_TICKERS = ["0050", "00878"]
US_TICKERS = ["VOO", "SCHD"]

_args = sys.argv[1:]
_i = 0
while _i < len(_args):
    if _args[_i] == "--tw" and _i + 1 < len(_args):
        TW_TICKERS = _args[_i + 1].split(",")
        _i += 2
    elif _args[_i] == "--us" and _i + 1 < len(_args):
        US_TICKERS = _args[_i + 1].split(",")
        _i += 2
    else:
        _i += 1

# ──────────────────────────────────────────
# 靜態 AUM 對照表（與 main.py 同步，單位：元）
# Yahoo 抓不到時用此備援
# ──────────────────────────────────────────
STATIC_AUM = {
    '0050':    3200e8,
    '0056':    2100e8,
    '00878':   1800e8,
    '006208':   850e8,
    '00919':    750e8,
    '00929':    620e8,
    '00713':    520e8,
    '00940':    430e8,
    '00939':    380e8,
    '0052':     180e8,
    '00692':    280e8,
    '00679B':   220e8,
    '00687B':   160e8,
    '00751B':    80e8,
    '006205':    30e8,
}

# ──────────────────────────────────────────
# 常數
# ──────────────────────────────────────────
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "
INFO = "ℹ️ "

YAHOO_SLEEP = 5    # Yahoo 每次請求間隔秒數
MAX_RETRY   = 3    # Yahoo 429 最大重試次數

UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; rv:125.0) Gecko/20100101 Firefox/125.0",
]

# ──────────────────────────────────────────
# Session 工廠
# ──────────────────────────────────────────
def yahoo_session(referer=None):
    """Yahoo 用：帶 gzip 壓縮"""
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,ashandler/x-json, */*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
<<<<<<< HEAD
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
=======
        "Cache-Control":   "no-cache",
        "Origin":          "https://finance.yahoo.com",
>>>>>>> 64cb4cc (修改存股回測功能)
    })
    if referer:
        s.headers["Referer"] = referer
    return s


def twse_session(referer=None):
    """TWSE 用：不帶 Accept-Encoding，避免 gzip 壓縮後 JSON 解析失敗"""
    s = requests.Session()
    s.headers.update({
        "User-Agent":      random.choice(UA_POOL),
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Cache-Control":   "no-cache",
    })
    if referer:
        s.headers["Referer"] = referer
    return s


# ──────────────────────────────────────────
# 工具函式
# ──────────────────────────────────────────
def safe_float(v, default=0.0):
    try:
        f = float(str(v).replace(",", ""))
        return f if f == f else default
    except Exception:
        return default


def annualized_return(closes, years):
    if not closes or len(closes) < 5:
        return None
    try:
        p0, p1 = float(closes[0]), float(closes[-1])
        if p0 <= 0:
            return None
        total = (p1 - p0) / p0
        if years < 1:
            return round(total * 100, 2)
        return round(((1 + total) ** (1 / years) - 1) * 100, 2)
    except Exception:
        return None


def safe_json(r):
    """解析 JSON，失敗時印出原始回傳供排查"""
    try:
        return r.json()
    except Exception as e:
        ct  = r.headers.get("Content-Type", "?")
        raw = r.text[:400].strip()
        print(f"  {WARN} JSON 解析失敗：{e}")
        print(f"       Content-Type：{ct}")
        print(f"       原始回傳（前400字）：{repr(raw) if raw else '(空白)'}")
        return None


def yahoo_get(url, referer, timeout=15):
    """
    帶 429 自動重試的 Yahoo GET
    回傳 (response, status_code, error_msg)
    重試等待：10s → 20s → 40s
    """
    for attempt in range(MAX_RETRY):
        try:
            s = yahoo_session(referer)
            r = s.get(url, timeout=timeout)
            if r.status_code == 429:
                wait = 10 * (2 ** attempt)
                print(f"  {WARN} Yahoo 429，第 {attempt+1}/{MAX_RETRY} 次重試，等 {wait}s...")
                time.sleep(wait)
                continue
            return r, r.status_code, None
        except Exception as e:
            if attempt < MAX_RETRY - 1:
                time.sleep(5)
            else:
                return None, 0, str(e)
    return None, 429, f"超過最大重試次數（{MAX_RETRY} 次，仍為 429）"


def yahoo_ticker_tw(ticker):
    return f"{ticker}.TWO" if ticker.upper().endswith("B") else f"{ticker}.TW"


# ──────────────────────────────────────────
# 結果追蹤
# ──────────────────────────────────────────
results = {}

def record_ok(ticker, field, value, note=""):
    results.setdefault(ticker, {})[field] = (PASS, value, note)

def record_fail(ticker, field, note=""):
    results.setdefault(ticker, {})[field] = (FAIL, None, note)

def record(ticker, field, value, note=""):
    ok = value is not None and value != 0 and value != "" and value != "不配息"
    results.setdefault(ticker, {})[field] = (PASS if ok else FAIL, value, note)


def header(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print('='*65)

def subheader(title):
    print(f"\n  ── {title} ──")

def field_line(status, field, value, note=""):
    val_str  = str(value) if value is not None else "(無)"
    note_str = f"  [{note}]" if note else ""
    print(f"  {status}  {field:<34} {val_str}{note_str}")


# ══════════════════════════════════════════
#  台股診斷
# ══════════════════════════════════════════
def diagnose_tw(ticker):
    yt = yahoo_ticker_tw(ticker)
    header(f"台股 ETF：{ticker}  ({yt})")
    price = 0.0

    # ── A. 即時報價（TWSE → TPEX 備援）──────────────────────────────
    subheader("A. 即時報價（TWSE / TPEX MIS）")
    items, source = [], "TWSE"
    try:
        s = twse_session("https://mis.twse.com.tw/")
        r = s.get(f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{ticker}.tw&json=1&delay=0", timeout=8)
        print(f"  {INFO} TWSE MIS HTTP {r.status_code}")
        j = safe_json(r)
        items = (j or {}).get("msgArray", [])
    except Exception as e:
        print(f"  {WARN} TWSE MIS 例外：{e}")

    if not items:
        print(f"  {WARN} TWSE 無資料，改試 TPEX...")
        try:
            s2 = twse_session("https://mis.tpex.org.tw/")
            r2 = s2.get(f"https://mis.tpex.org.tw/stock/api/getStockInfo.jsp?ex_ch=otc_{ticker}.tw&json=1&delay=0", timeout=8)
            print(f"  {INFO} TPEX MIS HTTP {r2.status_code}")
            j2 = safe_json(r2)
            items = (j2 or {}).get("msgArray", [])
            source = "TPEX"
        except Exception as e:
            print(f"  {WARN} TPEX MIS 例外：{e}")

    if items:
        d = items[0]
        z, y = d.get("z", "-"), d.get("y", "0")
        h, l_v = d.get("h", "-"), d.get("l", "-")
        v, name = d.get("v", "0"), d.get("n", "")

        price = safe_float(z if z != "-" else y)
        prev  = safe_float(y) if y != "-" else price
        high  = safe_float(h) if h != "-" else price
        low   = safe_float(l_v) if l_v != "-" else price
        vol   = int(safe_float(v) * 1000)
        chg   = round(price - prev, 4) if prev > 0 else 0
        chg_p = round(chg / prev * 100, 4) if prev > 0 else 0

        note_p = f"來源={source}" + ("" if z != "-" else "，非交易時段用昨收")
        field_line(PASS if price > 0 else FAIL, "current_price",        price, note_p)
        field_line(PASS if prev > 0  else FAIL, "prev_close",           prev,  "昨收")
        field_line(PASS,                         "price_change",         chg)
        field_line(PASS,                         "price_change_percent", f"{chg_p}%")
        field_line(PASS if high > 0  else WARN,  "day_high",            high)
        field_line(PASS if low > 0   else WARN,  "day_low",             low)
        field_line(PASS if vol > 0   else WARN,  "volume",              vol,   "千股*1000")
        field_line(PASS if name      else FAIL,  "name",                name)

        record_ok(ticker, "current_price",        price, note_p)
        record_ok(ticker, "price_change",         chg)
        record_ok(ticker, "price_change_percent", chg_p)
        record_ok(ticker, "day_high",             high)
        record_ok(ticker, "day_low",              low)
        record_ok(ticker, "volume",               vol)
    else:
        for f in ["current_price","price_change","price_change_percent","day_high","day_low","volume"]:
            field_line(FAIL, f, None, "MIS 全部失敗")
            record_fail(ticker, f, "MIS 全部失敗")

    time.sleep(YAHOO_SLEEP)

    # ── B. 配息 + 歷史月線（一次請求同時拿，減少 429）──────────────
    subheader("B. 配息 + 歷史月線（Yahoo v8 chart 5y + dividends）")
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=5y&interval=1mo&events=dividends&includePrePost=false"
    r, code, err = yahoo_get(url, f"https://finance.yahoo.com/quote/{yt}")
    print(f"  {INFO} Yahoo chart HTTP {code}")

    closes_5y = []
    if r and code == 200:
        j = safe_json(r)
        result = (j or {}).get("chart", {}).get("result")
        if result:
            # ── 歷史月線 ──
            quotes    = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes_5y = [safe_float(c) for c in (quotes.get("close") or []) if c is not None]
            n = len(closes_5y)
            field_line(PASS if n >= 12 else WARN, "月線資料筆數", n, "需≥12才能算1y")

            r1y = annualized_return(closes_5y[-12:] if n >= 12 else closes_5y, 1)
            r3y = annualized_return(closes_5y[-36:] if n >= 36 else closes_5y, 3)
            r5y = annualized_return(closes_5y, 5)
            field_line(PASS if r1y is not None else WARN, "annual_return_1y", f"{r1y}%")
            field_line(PASS if r3y is not None else WARN, "annual_return_3y", f"{r3y}%")
            field_line(PASS if r5y is not None else WARN, "annual_return_5y", f"{r5y}%")
            record_ok(ticker, "annual_return_1y", r1y)
            record_ok(ticker, "annual_return_3y", r3y)
            record_ok(ticker, "annual_return_5y", r5y)

            # 52週高低（由月線推算）
            last12 = closes_5y[-12:] if len(closes_5y) >= 12 else closes_5y
            if last12:
                w52h, w52l = max(last12), min(last12)
                field_line(PASS, "fifty_two_week_high", w52h, "月線推算")
                field_line(PASS, "fifty_two_week_low",  w52l, "月線推算")
                record_ok(ticker, "fifty_two_week_high", w52h)
                record_ok(ticker, "fifty_two_week_low",  w52l)

            # ── 配息 ──
            meta   = result[0].get("meta", {})
            events = result[0].get("events", {}).get("dividends", {})
            cutoff = time.time() - 365 * 86400
            recent = [v["amount"] for v in events.values()
                      if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
            n_div = len(recent)
            total_div = sum(recent)
            yf_yield_meta = safe_float(meta.get("dividendYield") or 0) * 100
            calc_yield    = round(total_div / price * 100, 4) if price > 0 and total_div > 0 else 0
            div_yield     = max(calc_yield, round(yf_yield_meta, 4))
            freq = ("月配" if n_div >= 10 else "季配" if n_div >= 3 else
                    "半年配" if n_div == 2 else "年配" if n_div == 1 else
                    "季配" if div_yield > 0 else "不配息")

            field_line(PASS if div_yield > 0 else WARN, "dividend_yield",
                       f"{div_yield}%", f"近1年{n_div}次，合計={total_div:.4f}")
            field_line(PASS if n_div > 0 else WARN, "payout_freq", freq)
            record_ok(ticker, "dividend_yield", div_yield)
            record_ok(ticker, "payout_freq",    freq)

            if events:
                sorted_ev = sorted(events.values(), key=lambda x: x["date"])[-4:]
                print(f"  {INFO} 最近配息：")
                for ev in sorted_ev:
                    dt = datetime.datetime.fromtimestamp(ev["date"]).strftime("%Y-%m-%d")
                    print(f"       {dt}  {ev['amount']:.4f} 元/股")
        else:
            field_line(FAIL, "Yahoo chart result", None, "result 為空")
    else:
        field_line(FAIL, "Yahoo chart", None, err or f"HTTP {code}")
        for f in ["annual_return_1y","annual_return_3y","annual_return_5y","dividend_yield"]:
            record_fail(ticker, f, err or f"HTTP {code}")

    time.sleep(YAHOO_SLEEP)

    # ── C. 配息備援：TWSE TWT48U（不帶 gzip）────────────────────────
    subheader("C. 配息備援（TWSE TWT48U，不帶 gzip）")
    try:
        s = twse_session("https://www.twse.com.tw/")
        r = s.get(f"https://www.twse.com.tw/exchangeReport/TWT48U?response=json&stockNo={ticker}", timeout=10)
        print(f"  {INFO} TWT48U HTTP {r.status_code}  CT: {r.headers.get('Content-Type','?')}")
        j = safe_json(r)
        if j:
            rows = j.get("data", [])
            field_line(PASS if rows else WARN, "TWT48U 資料筆數", len(rows))
            if rows:
                print(f"  {INFO} 最近 3 筆：")
                for row in rows[-3:]:
                    print(f"       {row}")
                record_ok(ticker, "dividend_backup_twt48u", len(rows), "備援可用")
            else:
                record_fail(ticker, "dividend_backup_twt48u", "無資料")
        else:
            record_fail(ticker, "dividend_backup_twt48u", "JSON 解析失敗")
    except Exception as e:
        field_line(FAIL, "TWT48U", None, str(e))

    time.sleep(YAHOO_SLEEP)

    # ── D. 詳細資料：asset_size / pe_ratio / expense_ratio ──────────
    subheader("D. 詳細資料（asset_size / pe_ratio / expense_ratio）")
    asset_size    = 0.0
    pe_ratio      = 0.0
    expense_ratio = 0.0

    # D1. TWSE fundInfo（不帶 gzip）
    print(f"  {INFO} D1. TWSE fundInfo")
    try:
        s = twse_session("https://www.twse.com.tw/")
        r = s.get(f"https://www.twse.com.tw/fund/ETF/fundInfo?response=json&stockNo={ticker}", timeout=10)
        print(f"       HTTP {r.status_code}  CT: {r.headers.get('Content-Type','?')}")
        j = safe_json(r)
        if j:
            rows = j.get("data") or []
            if not rows and j.get("tables"):
                rows = j["tables"][0].get("data", [])
            for row in rows:
                for cell in (row if isinstance(row, list) else []):
                    try:
                        val = float(str(cell).replace(",","").strip())
                        if val > 1e6:
                            asset_size = val * 10000; break
                        elif val > 1e4:
                            asset_size = val * 1e8;   break
                    except Exception:
                        pass
                if asset_size: break
            if asset_size:
                field_line(PASS, "asset_size (D1)", f"{asset_size/1e8:.1f} 億")
            else:
                print(f"       {WARN} 無法解析規模，原始前2筆：{rows[:2]}")
        else:
            print(f"       {WARN} JSON 失敗，見上方原始回傳")
    except Exception as e:
        print(f"       {FAIL} {e}")

    # D2. yfinance Ticker.info
    if not asset_size or not expense_ratio:
        print(f"  {INFO} D2. yfinance Ticker.info ({yt})")
        try:
            import yfinance as yf
            info = yf.Ticker(yt).info or {}
            if info.get("regularMarketPrice"):
                print(f"       {PASS} yfinance 正常，keys={len(info)}")
                if not asset_size:
                    for k in ("totalAssets","netAssets","totalNetAssets"):
                        v = safe_float(info.get(k) or 0)
                        if v > 0:
                            asset_size = v
                            field_line(PASS, f"asset_size (D2 yf/{k})", f"{asset_size/1e8:.1f} 億")
                            break
                if not pe_ratio:
                    for k in ("trailingPE","forwardPE"):
                        v = safe_float(info.get(k) or 0)
                        if v > 0:
                            pe_ratio = v
                            field_line(PASS, f"pe_ratio (D2 yf/{k})", pe_ratio)
                            break
                if not expense_ratio:
                    for k in ("annualReportExpenseRatio","expenseRatio"):
                        v = safe_float(info.get(k) or 0)
                        if v > 0:
                            expense_ratio = v
                            field_line(PASS, f"expense_ratio (D2 yf/{k})", f"{v*100:.4f}%")
                            break
            else:
                print(f"       {WARN} 無 regularMarketPrice，keys={list(info.keys())[:8]}")
        except ImportError:
            print(f"       {WARN} yfinance 未安裝")
        except Exception as e:
            print(f"       {FAIL} {e}")

    # D3. Yahoo quoteSummary 備援
    if not asset_size or not expense_ratio:
        print(f"  {INFO} D3. Yahoo quoteSummary 備援")
        url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yt}?modules=summaryDetail,defaultKeyStatistics,fundProfile"
        r, code, err = yahoo_get(url, f"https://finance.yahoo.com/quote/{yt}")
        print(f"       HTTP {code}")
        if r and code == 200:
            j = safe_json(r)
            qs  = (j or {}).get("quoteSummary", {}).get("result", [{}])
            raw = qs[0] if qs else {}
            if not asset_size:
                for sec in ("summaryDetail","defaultKeyStatistics"):
                    for k in ("totalAssets","netAssets"):
                        v = raw.get(sec, {}).get(k)
                        if isinstance(v, dict) and "raw" in v:
                            val = safe_float(v["raw"])
                            if val > 0:
                                asset_size = val
                                field_line(PASS, f"asset_size (D3/{sec}.{k})", f"{val/1e8:.1f} 億")
            if not expense_ratio:
                fees = raw.get("fundProfile", {}).get("feesExpensesInvestment", {})
                for k in ("annualReportExpenseRatioNet","annualReportExpenseRatio","netExpenseRatio"):
                    v = fees.get(k)
                    if isinstance(v, dict) and "raw" in v:
                        val = safe_float(v["raw"])
                        if val > 0:
                            expense_ratio = val
                            field_line(PASS, f"expense_ratio (D3/fees.{k})", f"{val*100:.4f}%")
                            break
        else:
            print(f"       {FAIL} {err or f'HTTP {code}'}")

    # D4. 靜態 AUM 最終備援
    if not asset_size and ticker in STATIC_AUM:
        asset_size = STATIC_AUM[ticker]
        field_line(WARN, "asset_size (D4 靜態備援)", f"{asset_size/1e8:.1f} 億", "可能非最新值")

    print(f"\n  ── 詳細欄位最終結果 ──")
    field_line(PASS if asset_size    > 0 else FAIL, "asset_size    最終", f"{asset_size/1e8:.1f} 億" if asset_size else None)
    field_line(PASS if pe_ratio      > 0 else WARN, "pe_ratio      最終", pe_ratio    if pe_ratio      else None, "台股ETF通常為0")
    field_line(PASS if expense_ratio > 0 else WARN, "expense_ratio 最終", f"{expense_ratio*100:.4f}%" if expense_ratio else None)

    record(ticker, "asset_size",    asset_size)
    record(ticker, "pe_ratio",      pe_ratio)
    record(ticker, "expense_ratio", expense_ratio)

    time.sleep(YAHOO_SLEEP)


# ══════════════════════════════════════════
#  美股診斷
# ══════════════════════════════════════════
def diagnose_us(ticker):
    header(f"美股 ETF：{ticker}")
    price = 0.0

    # ── A. 即時報價（Yahoo v8 chart 10d）────────────────────────────
    subheader("A. 即時報價（Yahoo v8 chart 10d）")
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=10d&interval=1d&includePrePost=false"
    r, code, err = yahoo_get(url, f"https://finance.yahoo.com/quote/{ticker}")
    print(f"  {INFO} Yahoo chart HTTP {code}")

    if r and code == 200:
        j = safe_json(r)
        result = (j or {}).get("chart", {}).get("result")
        if result:
            meta    = result[0].get("meta", {})
            quotes  = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes  = [c for c in (quotes.get("close")  or []) if c is not None]
            highs   = [h for h in (quotes.get("high")   or []) if h is not None]
            lows    = [l for l in (quotes.get("low")    or []) if l is not None]
            volumes = [v for v in (quotes.get("volume") or []) if v is not None]

            meta_p = safe_float(meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or 0)
            prev_c = safe_float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
            w52h   = safe_float(meta.get("fiftyTwoWeekHigh") or 0)
            w52l   = safe_float(meta.get("fiftyTwoWeekLow")  or 0)

            if len(closes) >= 2:
                price = safe_float(closes[-1]); prev = safe_float(closes[-2])
            elif meta_p > 0:
                price = meta_p; prev = prev_c if prev_c > 0 else meta_p
            else:
                price = prev = 0.0

            chg   = round(price - prev, 4) if prev > 0 else 0
            chg_p = round(chg / prev * 100, 4) if prev > 0 else 0

            field_line(PASS if price > 0 else FAIL, "current_price",        price)
            field_line(PASS if prev > 0  else FAIL, "prev_close",           prev)
            field_line(PASS,                         "price_change",         chg)
            field_line(PASS,                         "price_change_percent", f"{chg_p}%")
            field_line(PASS if highs   else WARN,    "day_high",  safe_float(highs[-1])  if highs   else None)
            field_line(PASS if lows    else WARN,    "day_low",   safe_float(lows[-1])   if lows    else None)
            field_line(PASS if volumes else WARN,    "volume",    int(volumes[-1])        if volumes else None)
            field_line(PASS if w52h > 0 else WARN,   "fifty_two_week_high", w52h, "meta")
            field_line(PASS if w52l > 0 else WARN,   "fifty_two_week_low",  w52l, "meta")

            record_ok(ticker, "current_price",        price)
            record_ok(ticker, "price_change",         chg)
            record_ok(ticker, "price_change_percent", chg_p)
            record_ok(ticker, "day_high",  safe_float(highs[-1])  if highs   else 0)
            record_ok(ticker, "day_low",   safe_float(lows[-1])   if lows    else 0)
            record_ok(ticker, "volume",    int(volumes[-1])        if volumes else 0)
            if w52h > 0: record_ok(ticker, "fifty_two_week_high", w52h)
            if w52l > 0: record_ok(ticker, "fifty_two_week_low",  w52l)
        else:
            field_line(FAIL, "current_price", None, "result 為空")
            record_fail(ticker, "current_price", "result 為空")
    else:
        field_line(FAIL, "current_price", None, err or f"HTTP {code}")
        record_fail(ticker, "current_price", err or f"HTTP {code}")

    time.sleep(YAHOO_SLEEP)

    # ── B. 配息 + 歷史月線（一次請求）──────────────────────────────
    subheader("B. 配息 + 歷史月線（Yahoo v8 chart 5y + dividends）")
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1mo&events=dividends&includePrePost=false"
    r, code, err = yahoo_get(url, f"https://finance.yahoo.com/quote/{ticker}")
    print(f"  {INFO} Yahoo chart HTTP {code}")

    if r and code == 200:
        j = safe_json(r)
        result = (j or {}).get("chart", {}).get("result")
        if result:
            # 歷史月線
            quotes    = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes_5y = [safe_float(c) for c in (quotes.get("close") or []) if c is not None]
            n = len(closes_5y)
            field_line(PASS if n >= 12 else WARN, "月線資料筆數", n)

            r1y = annualized_return(closes_5y[-12:] if n >= 12 else closes_5y, 1)
            r3y = annualized_return(closes_5y[-36:] if n >= 36 else closes_5y, 3)
            r5y = annualized_return(closes_5y, 5)
            field_line(PASS if r1y is not None else WARN, "annual_return_1y", f"{r1y}%")
            field_line(PASS if r3y is not None else WARN, "annual_return_3y", f"{r3y}%")
            field_line(PASS if r5y is not None else WARN, "annual_return_5y", f"{r5y}%")
            record_ok(ticker, "annual_return_1y", r1y)
            record_ok(ticker, "annual_return_3y", r3y)
            record_ok(ticker, "annual_return_5y", r5y)

            # 配息
            events = result[0].get("events", {}).get("dividends", {})
            cutoff = time.time() - 365 * 86400
            recent = [v["amount"] for v in events.values()
                      if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
            n_div     = len(recent)
            total_div = sum(recent)
            div_yield = round(total_div / price * 100, 4) if price > 0 and total_div > 0 else 0
            freq = ("月配" if n_div >= 10 else "季配" if n_div >= 3 else
                    "半年配" if n_div == 2 else "年配" if n_div == 1 else "不配息")

            field_line(PASS if div_yield > 0 else WARN, "dividend_yield",
                       f"{div_yield}%", f"近1年{n_div}次，合計=${total_div:.4f}")
            field_line(PASS if n_div > 0 else WARN, "payout_freq", freq)
            record_ok(ticker, "dividend_yield", div_yield)
            record_ok(ticker, "payout_freq",    freq)

            if events:
                sorted_ev = sorted(events.values(), key=lambda x: x["date"])[-4:]
                print(f"  {INFO} 最近配息：")
                for ev in sorted_ev:
                    dt = datetime.datetime.fromtimestamp(ev["date"]).strftime("%Y-%m-%d")
                    print(f"       {dt}  ${ev['amount']:.4f}")
        else:
            field_line(FAIL, "月線/配息", None, "result 為空")
    else:
        field_line(FAIL, "月線/配息", None, err or f"HTTP {code}")
        for f in ["annual_return_1y","annual_return_3y","annual_return_5y","dividend_yield"]:
            record_fail(ticker, f, err or f"HTTP {code}")

    time.sleep(YAHOO_SLEEP)

    # ── C. 詳細資料（Yahoo quoteSummary）───────────────────────────
    subheader("C. 詳細資料（Yahoo quoteSummary）")
    asset_size    = 0.0
    pe_ratio      = 0.0
    expense_ratio = 0.0

    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
           f"?modules=summaryDetail,defaultKeyStatistics,fundProfile")
    r, code, err = yahoo_get(url, f"https://finance.yahoo.com/quote/{ticker}")
    print(f"  {INFO} quoteSummary HTTP {code}")

    if r and code == 200:
        j   = safe_json(r)
        qs  = (j or {}).get("quoteSummary", {}).get("result", [{}])
        raw = qs[0] if qs else {}

        merged = {}
        for section in raw.values():
            if isinstance(section, dict):
                for k, v in section.items():
                    if isinstance(v, dict) and "raw" in v:
                        merged[k] = v["raw"]
                    elif not isinstance(v, (dict, list)):
                        merged[k] = v

        for k in ("totalAssets","netAssets"):
            v = merged.get(k)
            if v and safe_float(v) > 0:
                asset_size = safe_float(v)
                field_line(PASS, f"asset_size [{k}]", f"${asset_size/1e9:.2f}B")
                break

        for k in ("trailingPE","forwardPE"):
            v = merged.get(k)
            if v and safe_float(v) > 0:
                pe_ratio = safe_float(v)
                field_line(PASS, f"pe_ratio [{k}]", pe_ratio)
                break

        fees = raw.get("fundProfile", {}).get("feesExpensesInvestment", {})
        for k in ("annualReportExpenseRatioNet","annualReportExpenseRatio","netExpenseRatio"):
            v = fees.get(k)
            if isinstance(v, dict) and "raw" in v:
                val = safe_float(v["raw"])
                if val > 0:
                    expense_ratio = val
                    field_line(PASS, f"expense_ratio [fees.{k}]", f"{val*100:.4f}%")
                    break
            elif v and safe_float(v) > 0:
                expense_ratio = safe_float(v)
                field_line(PASS, f"expense_ratio [fees.{k}]", f"{expense_ratio*100:.4f}%")
                break
        if not expense_ratio:
            for k in ("annualReportExpenseRatio","expenseRatio"):
                v = merged.get(k)
                if v and safe_float(v) > 0:
                    expense_ratio = safe_float(v)
                    field_line(PASS, f"expense_ratio [merged.{k}]", f"{expense_ratio*100:.4f}%")
                    break

        # 52週高低備援（若 A 段 meta 沒拿到）
        w52h = safe_float(merged.get("fiftyTwoWeekHigh") or 0)
        w52l = safe_float(merged.get("fiftyTwoWeekLow")  or 0)
        if w52h > 0: field_line(PASS, "fifty_two_week_high (quoteSummary)", w52h)
        if w52l > 0: field_line(PASS, "fifty_two_week_low  (quoteSummary)", w52l)

        field_line(PASS if asset_size    > 0 else FAIL, "asset_size    最終", f"${asset_size/1e9:.2f}B" if asset_size else None)
        field_line(PASS if pe_ratio      > 0 else WARN, "pe_ratio      最終", pe_ratio    if pe_ratio      else None, "部分ETF無PE")
        field_line(PASS if expense_ratio > 0 else FAIL, "expense_ratio 最終", f"{expense_ratio*100:.4f}%" if expense_ratio else None)
    else:
        field_line(FAIL, "quoteSummary", None, err or f"HTTP {code}")

    record(ticker, "asset_size",    asset_size)
    record(ticker, "pe_ratio",      pe_ratio)
    record(ticker, "expense_ratio", expense_ratio)

    time.sleep(YAHOO_SLEEP)


# ══════════════════════════════════════════
#  總結報告
# ══════════════════════════════════════════
def print_summary():
    header("📊  診斷總結報告")

    critical_fields  = ["current_price","price_change","price_change_percent",
                        "volume","day_high","day_low",
                        "dividend_yield","payout_freq","annual_return_1y","asset_size"]
    important_fields = ["annual_return_3y","annual_return_5y",
                        "expense_ratio","pe_ratio",
                        "fifty_two_week_high","fifty_two_week_low"]

    all_pass = True
    for ticker, fields in results.items():
        print(f"\n  【{ticker}】")
        has_fail = False
        for field, (status, value, note) in fields.items():
            tag      = " ★" if field in critical_fields else (" ☆" if field in important_fields else "  ")
            val_str  = str(value) if value is not None else "(無)"
            note_str = f"  {note}" if note else ""
            print(f"    {status} {field:<36}{tag}  {val_str}{note_str}")
            if status == FAIL:
                has_fail = True
                all_pass = False
        print(f"    {'⚠️  有欄位失敗' if has_fail else '✅ 所有欄位正常'}")

    print(f"\n{'='*65}")
    if all_pass:
        print(f"  {PASS}  全部代碼診斷通過！")
    else:
        print(f"  {WARN}  有欄位未能正常抓取。★=核心欄位  ☆=重要欄位")
    print('='*65)


# ══════════════════════════════════════════
#  主程式
# ══════════════════════════════════════════
if __name__ == "__main__":
    print("="*65)
    print("  ETF 系統全欄位診斷腳本 v4")
    print(f"  台股：{', '.join(TW_TICKERS)}")
    print(f"  美股：{', '.join(US_TICKERS)}")
    print(f"  時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Yahoo 間隔：{YAHOO_SLEEP}s  429重試：{MAX_RETRY}次")
    print("="*65)

    for t in TW_TICKERS:
        diagnose_tw(t.strip())
        time.sleep(3)

    for t in US_TICKERS:
        diagnose_us(t.strip())
        time.sleep(3)

    print_summary()
