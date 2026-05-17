"""
ETF 系統全欄位診斷腳本 v2
用法：python diagnose.py [--tw 代碼] [--us 代碼]
預設測試：台股 0050、00878，美股 VOO、SCHD

測試項目對應 main.py 實際需要的欄位：
  ✦ current_price        成交價（MIS 即時 / Yahoo meta）
  ✦ price_change         漲跌（需要昨收）
  ✦ price_change_percent 漲跌幅
  ✦ volume               成交量
  ✦ day_high / day_low   當日最高最低
  ✦ asset_size           基金規模（TWSE fundInfo / Yahoo totalAssets）
  ✦ dividend_yield       殖利率（Yahoo events / TWSE TWT48U）
  ✦ payout_freq          配息頻率
  ✦ annual_return_1y/3y/5y  年化報酬率（Yahoo 月線歷史）
  ✦ pe_ratio             本益比
  ✦ expense_ratio        費用率
  ✦ fifty_two_week_high/low  52 週高低
"""

import sys, time, random, datetime, json

try:
    import requests
except ImportError:
    sys.exit("❌  請先安裝 requests：pip install requests")

# ──────────────────────────────────────────
# 命令列參數（選用）
# ──────────────────────────────────────────
TW_TICKERS = ["0050", "00878"]
US_TICKERS = ["VOO", "SCHD"]

args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--tw" and i + 1 < len(args):
        TW_TICKERS = args[i + 1].split(",")
        i += 2
    elif args[i] == "--us" and i + 1 < len(args):
        US_TICKERS = args[i + 1].split(",")
        i += 2
    else:
        i += 1

# ──────────────────────────────────────────
# 工具函式
# ──────────────────────────────────────────
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def new_session(referer=None):
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,ashandler/x-json, */*;q=0.8",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    })
    if referer:
        s.headers["Referer"] = referer
    return s

def safe_float(v, default=0.0):
    try:
        f = float(str(v).replace(",", ""))
        return f if f == f else default  # NaN guard
    except Exception:
        return default

def annualized_return(closes, years):
    """計算年化報酬率"""
    if not closes or len(closes) < 5:
        return None
    try:
        p0, p1 = float(closes[0]), float(closes[-1])
        if p0 <= 0:
            return None
        total = (p1 - p0) / p0
        if years < 1:
            return round(total * 100, 2)
        ann = ((1 + total) ** (1 / years)) - 1
        return round(ann * 100, 2)
    except Exception:
        return None

# ──────────────────────────────────────────
# 結果追蹤
# ──────────────────────────────────────────
PASS = "✅"
FAIL = "❌"
WARN = "⚠️ "
INFO = "ℹ️ "

results = {}   # ticker → {field: (status, value, note)}

def record(ticker, field, value, note=""):
    """記錄一個欄位的檢測結果"""
    if ticker not in results:
        results[ticker] = {}
    ok = value is not None and value != 0 and value != "" and value != "不配息"
    status = PASS if ok else FAIL
    results[ticker][field] = (status, value, note)

def record_ok(ticker, field, value, note=""):
    if ticker not in results:
        results[ticker] = {}
    results[ticker][field] = (PASS, value, note)

def record_fail(ticker, field, note=""):
    if ticker not in results:
        results[ticker] = {}
    results[ticker][field] = (FAIL, None, note)

def header(title):
    print(f"\n{'='*65}")
    print(f"  {title}")
    print('='*65)

def subheader(title):
    print(f"\n  ── {title} ──")

def field_line(status, field, value, note=""):
    val_str = str(value) if value is not None else "(無)"
    note_str = f"  [{note}]" if note else ""
    print(f"  {status}  {field:<30} {val_str}{note_str}")

# ══════════════════════════════════════════
#  台股診斷
# ══════════════════════════════════════════

def yahoo_ticker_tw(ticker):
    return f"{ticker}.TWO" if ticker.upper().endswith("B") else f"{ticker}.TW"

def diagnose_tw(ticker):
    yt = yahoo_ticker_tw(ticker)
    header(f"台股 ETF：{ticker}  ({yt})")
    price = 0.0

    # ────────────────────────────────────────
    # [A] 即時報價：TWSE MIS → TPEX MIS 備援
    # ────────────────────────────────────────
    subheader("A. 即時報價（TWSE / TPEX MIS）")
    items = []
    source = "TWSE"
    try:
        s = new_session("https://mis.twse.com.tw/")
        url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{ticker}.tw&json=1&delay=0"
        r = s.get(url, timeout=8)
        print(f"  {INFO} TWSE MIS HTTP {r.status_code}")
        items = r.json().get("msgArray", [])
    except Exception as e:
        print(f"  {WARN} TWSE MIS 例外：{e}")

    if not items:
        print(f"  {WARN} TWSE 無資料，改試 TPEX MIS...")
        try:
            s2 = new_session("https://mis.tpex.org.tw/")
            url2 = f"https://mis.tpex.org.tw/stock/api/getStockInfo.jsp?ex_ch=otc_{ticker}.tw&json=1&delay=0"
            r2 = s2.get(url2, timeout=8)
            print(f"  {INFO} TPEX MIS HTTP {r2.status_code}")
            items = r2.json().get("msgArray", [])
            source = "TPEX"
        except Exception as e:
            print(f"  {WARN} TPEX MIS 例外：{e}")

    if items:
        d = items[0]
        z = d.get("z", "-"); y = d.get("y", "0")
        h = d.get("h", "-"); l = d.get("l", "-")
        v = d.get("v", "0"); name = d.get("n", "")

        price_raw = z if z != "-" else y
        price = safe_float(price_raw)
        prev  = safe_float(y) if y != "-" else price
        high  = safe_float(h) if h != "-" else price
        low   = safe_float(l) if l != "-" else price
        vol   = int(safe_float(v) * 1000)
        chg   = round(price - prev, 4) if prev > 0 else 0
        chg_p = round(chg / prev * 100, 4) if prev > 0 else 0

        is_trading = z != "-"
        note_price = f"來源={source}" + ("" if is_trading else "，非交易時段用昨收")

        field_line(PASS if price > 0 else FAIL, "current_price",        price,  note_price)
        field_line(PASS if prev > 0  else FAIL, "prev_close (y)",       prev,   "昨收，用於計算漲跌")
        field_line(PASS, "price_change",          chg)
        field_line(PASS, "price_change_percent",  f"{chg_p}%")
        field_line(PASS if high > 0  else WARN, "day_high",             high,   "非交易時段可能等於 price")
        field_line(PASS if low > 0   else WARN, "day_low",              low,    "非交易時段可能等於 price")
        field_line(PASS if vol > 0   else WARN, "volume",               vol,    "千股*1000")
        field_line(PASS if name      else FAIL, "name (ETF名稱)",       name)

        record_ok(ticker, "current_price", price, note_price)
        record_ok(ticker, "price_change", chg)
        record_ok(ticker, "price_change_percent", chg_p)
        record_ok(ticker, "day_high", high)
        record_ok(ticker, "day_low", low)
        record_ok(ticker, "volume", vol)
    else:
        for f in ["current_price", "price_change", "price_change_percent", "day_high", "day_low", "volume"]:
            field_line(FAIL, f, None, "MIS 全部失敗")
            record_fail(ticker, f, "MIS 全部失敗")

    time.sleep(1)

    # ────────────────────────────────────────
    # [B] 配息：Yahoo v8 chart events=dividends
    # ────────────────────────────────────────
    subheader("B. 配息（Yahoo v8 chart events=dividends）")
    try:
        s = new_session(f"https://finance.yahoo.com/quote/{yt}")
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=2y&interval=1mo&events=dividends"
        r = s.get(url, timeout=12)
        print(f"  {INFO} Yahoo chart HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            result = j.get("chart", {}).get("result")
            if result:
                meta   = result[0].get("meta", {})
                events = result[0].get("events", {}).get("dividends", {})
                cutoff = time.time() - 365 * 86400
                recent = [v["amount"] for v in events.values()
                          if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
                n = len(recent)
                total_div = sum(recent)
                yf_yield_meta = safe_float(meta.get("dividendYield") or 0) * 100

                calc_yield = round(total_div / price * 100, 4) if price > 0 and total_div > 0 else 0
                div_yield  = max(calc_yield, round(yf_yield_meta, 4))
                freq = ("月配" if n >= 10 else "季配" if n >= 3 else "半年配" if n == 2
                        else "年配" if n == 1 else "季配" if div_yield > 0 else "不配息")

                field_line(PASS if div_yield > 0 else WARN, "dividend_yield",
                           f"{div_yield}%", f"近1年{n}次配息，合計={total_div:.4f}")
                field_line(PASS if n > 0 else WARN, "payout_freq", freq, f"配息次數={n}")
                field_line(INFO, "yf_meta_dividendYield", f"{yf_yield_meta:.4f}%", "Yahoo meta 備援")

                record_ok(ticker, "dividend_yield", div_yield)
                record_ok(ticker, "payout_freq", freq)

                # 顯示最近 4 筆
                if events:
                    sorted_ev = sorted(events.values(), key=lambda x: x["date"])[-4:]
                    print(f"  {INFO} 最近配息記錄：")
                    for ev in sorted_ev:
                        dt = datetime.datetime.fromtimestamp(ev["date"]).strftime("%Y-%m-%d")
                        print(f"       {dt}  {ev['amount']:.4f} 元/股")
            else:
                field_line(FAIL, "dividend_yield", None, "result 為空")
                record_fail(ticker, "dividend_yield", "result 為空")
        else:
            field_line(FAIL, "dividend_yield", None, f"HTTP {r.status_code}")
            record_fail(ticker, "dividend_yield", f"HTTP {r.status_code}")
    except Exception as e:
        field_line(FAIL, "dividend_yield", None, str(e))
        record_fail(ticker, "dividend_yield", str(e))

    time.sleep(1)

    # ────────────────────────────────────────
    # [C] 配息備援：TWSE TWT48U
    # ────────────────────────────────────────
    subheader("C. 配息備援（TWSE TWT48U 公告）")
    try:
        s = new_session("https://www.twse.com.tw/")
        url = f"https://www.twse.com.tw/exchangeReport/TWT48U?response=json&stockNo={ticker}"
        r = s.get(url, timeout=10)
        print(f"  {INFO} TWT48U HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            fields = j.get("fields", [])
            rows   = j.get("data", [])
            print(f"  {INFO} 欄位：{fields}")
            field_line(PASS if rows else WARN, "TWT48U 資料筆數", len(rows))
            if rows:
                print(f"  {INFO} 最近 3 筆：")
                for row in rows[-3:]:
                    print(f"       {row}")
                record_ok(ticker, "dividend_yield_twt48u_backup", len(rows), "備援來源可用")
            else:
                record_fail(ticker, "dividend_yield_twt48u_backup", "無資料")
        else:
            field_line(FAIL, "TWT48U", None, f"HTTP {r.status_code}")
    except Exception as e:
        field_line(FAIL, "TWT48U", None, str(e))

    time.sleep(1)

    # ────────────────────────────────────────
    # [D] 歷史月線 → 年化報酬率
    # ────────────────────────────────────────
    subheader("D. 歷史月線 → 年化報酬率（Yahoo v8 chart 5y 月線）")
    closes_5y = []
    try:
        s = new_session(f"https://finance.yahoo.com/quote/{yt}")
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=5y&interval=1mo"
        r = s.get(url, timeout=12)
        print(f"  {INFO} Yahoo 月線 HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            result = j.get("chart", {}).get("result")
            if result:
                quotes  = result[0].get("indicators", {}).get("quote", [{}])[0]
                closes_5y = [safe_float(c) for c in (quotes.get("close") or []) if c is not None]
                n = len(closes_5y)
                field_line(PASS if n >= 12 else WARN, "歷史月線資料筆數", n, "需要≥12才能算1年報酬")

                r1y = annualized_return(closes_5y[-12:] if n >= 12 else closes_5y, 1)
                r3y = annualized_return(closes_5y[-36:] if n >= 36 else closes_5y, 3)
                r5y = annualized_return(closes_5y, 5)

                field_line(PASS if r1y is not None else WARN, "annual_return_1y", f"{r1y}%")
                field_line(PASS if r3y is not None else WARN, "annual_return_3y", f"{r3y}%")
                field_line(PASS if r5y is not None else WARN, "annual_return_5y", f"{r5y}%")
                field_line(INFO, "最新5筆月線收盤", closes_5y[-5:] if closes_5y else "無")

                record_ok(ticker, "annual_return_1y", r1y)
                record_ok(ticker, "annual_return_3y", r3y)
                record_ok(ticker, "annual_return_5y", r5y)
            else:
                field_line(FAIL, "月線歷史", None, "result 為空")
                for f in ["annual_return_1y", "annual_return_3y", "annual_return_5y"]:
                    record_fail(ticker, f)
        else:
            field_line(FAIL, "月線歷史", None, f"HTTP {r.status_code}")
    except Exception as e:
        field_line(FAIL, "月線歷史", None, str(e))

    time.sleep(1)

    # ────────────────────────────────────────
    # [E] 詳細資料：TWSE fundInfo → yfinance → quoteSummary
    # ────────────────────────────────────────
    subheader("E. 詳細資料（asset_size / pe_ratio / expense_ratio）")

    asset_size   = 0.0
    pe_ratio     = 0.0
    expense_ratio = 0.0

    # E1. TWSE fundInfo
    print(f"  {INFO} E1. 試 TWSE /fund/ETF/fundInfo")
    try:
        s = new_session("https://www.twse.com.tw/")
        url = f"https://www.twse.com.tw/fund/ETF/fundInfo?response=json&stockNo={ticker}"
        r = s.get(url, timeout=10)
        print(f"       HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            rows = j.get("data") or []
            if not rows and j.get("tables"):
                rows = j["tables"][0].get("data", [])
            for row in rows:
                for cell in (row if isinstance(row, list) else []):
                    cell_str = str(cell).replace(",", "").strip()
                    try:
                        val = float(cell_str)
                        if val > 1e6:
                            asset_size = val * 10000
                            break
                        elif val > 1e4:
                            asset_size = val * 1e8
                            break
                    except Exception:
                        pass
                if asset_size:
                    break
            if asset_size:
                field_line(PASS, "asset_size (TWSE fundInfo)", f"{asset_size/1e8:.2f} 億元")
            else:
                print(f"       {WARN} 無法從 fundInfo 解析 asset_size")
                if rows:
                    print(f"       原始前2筆：{rows[:2]}")
    except Exception as e:
        print(f"       {FAIL} 例外：{e}")

    # E2. TWSE /ETF/fund/{ticker}
    if not asset_size:
        print(f"  {INFO} E2. 試 TWSE /ETF/fund/{ticker}")
        try:
            s2 = new_session("https://www.twse.com.tw/")
            url2 = f"https://www.twse.com.tw/ETF/fund/{ticker}"
            r2 = s2.get(url2, timeout=10)
            print(f"       HTTP {r2.status_code}")
            if r2.status_code == 200:
                j2 = r2.json()
                for key in ("totalAssets", "fundSize", "aum", "netAssets"):
                    v = j2.get(key)
                    if v:
                        val = safe_float(str(v).replace(",", ""))
                        if val > 0:
                            asset_size = val * 1000 if val < 1e8 else val
                            field_line(PASS, f"asset_size (TWSE ETF/fund [{key}])", f"{asset_size/1e8:.2f} 億元")
                            break
                if not asset_size:
                    print(f"       {WARN} 回傳 keys：{list(j2.keys())[:10]}")
        except Exception as e:
            print(f"       {FAIL} 例外：{e}")

    # E3. yfinance Ticker.info
    print(f"  {INFO} E3. 試 yfinance Ticker.info ({yt})")
    try:
        import yfinance as yf
        stock = yf.Ticker(yt)
        info  = stock.info or {}
        if info.get("regularMarketPrice"):
            print(f"       {PASS} yfinance 回傳正常，keys 數量：{len(info)}")
            if not asset_size:
                for k in ("totalAssets", "netAssets", "totalNetAssets"):
                    v = safe_float(info.get(k) or 0)
                    if v > 0:
                        asset_size = v
                        field_line(PASS, f"asset_size (yf.info [{k}])", f"{asset_size/1e8:.2f} 億元")
                        break
            if not pe_ratio:
                for k in ("trailingPE", "forwardPE"):
                    v = safe_float(info.get(k) or 0)
                    if v > 0:
                        pe_ratio = v
                        field_line(PASS, f"pe_ratio (yf.info [{k}])", pe_ratio)
                        break
            if not expense_ratio:
                for k in ("annualReportExpenseRatio", "expenseRatio"):
                    v = safe_float(info.get(k) or 0)
                    if v > 0:
                        expense_ratio = v
                        field_line(PASS, f"expense_ratio (yf.info [{k}])", f"{expense_ratio*100:.4f}%")
                        break
            # 52週高低
            w52h = safe_float(info.get("fiftyTwoWeekHigh") or 0)
            w52l = safe_float(info.get("fiftyTwoWeekLow")  or 0)
            if w52h > 0:
                field_line(PASS, "fifty_two_week_high (yf.info)", w52h)
                record_ok(ticker, "fifty_two_week_high", w52h)
            if w52l > 0:
                field_line(PASS, "fifty_two_week_low  (yf.info)", w52l)
                record_ok(ticker, "fifty_two_week_low", w52l)
        else:
            print(f"       {WARN} yfinance info 無 regularMarketPrice（可能被擋）")
            print(f"       info keys (前10)：{list(info.keys())[:10]}")
    except ImportError:
        print(f"       {WARN} yfinance 未安裝，跳過")
    except Exception as e:
        print(f"       {FAIL} 例外：{e}")

    # E4. Yahoo quoteSummary 備援
    if not asset_size or not expense_ratio:
        print(f"  {INFO} E4. 備援：Yahoo quoteSummary")
        try:
            s3 = new_session(f"https://finance.yahoo.com/quote/{yt}")
            url3 = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yt}"
                    f"?modules=summaryDetail,defaultKeyStatistics,fundProfile")
            r3 = s3.get(url3, timeout=12)
            print(f"       HTTP {r3.status_code}")
            if r3.status_code == 200:
                j3   = r3.json()
                qs   = j3.get("quoteSummary", {}).get("result", [{}])
                raw  = qs[0] if qs else {}
                if not asset_size:
                    for sec in ("summaryDetail", "defaultKeyStatistics"):
                        for k in ("totalAssets", "netAssets"):
                            v = raw.get(sec, {}).get(k)
                            if isinstance(v, dict) and "raw" in v:
                                val = safe_float(v["raw"])
                                if val > 0:
                                    asset_size = val
                                    field_line(PASS, f"asset_size (quoteSummary [{sec}.{k}])", f"{asset_size/1e8:.2f} 億元")
                if not pe_ratio:
                    for sec in ("summaryDetail", "defaultKeyStatistics"):
                        for k in ("trailingPE", "forwardPE"):
                            v = raw.get(sec, {}).get(k)
                            if isinstance(v, dict) and "raw" in v:
                                val = safe_float(v["raw"])
                                if val > 0:
                                    pe_ratio = val
                                    field_line(PASS, f"pe_ratio (quoteSummary [{sec}.{k}])", pe_ratio)
                if not expense_ratio:
                    fees = raw.get("fundProfile", {}).get("feesExpensesInvestment", {})
                    for k in ("annualReportExpenseRatioNet", "annualReportExpenseRatio", "netExpenseRatio"):
                        v = fees.get(k)
                        if isinstance(v, dict) and "raw" in v:
                            val = safe_float(v["raw"])
                            if val > 0:
                                expense_ratio = val
                                field_line(PASS, f"expense_ratio (quoteSummary [fees.{k}])", f"{expense_ratio*100:.4f}%")
                                break
            else:
                print(f"       HTTP {r3.status_code}")
        except Exception as e:
            print(f"       {FAIL} 例外：{e}")

    # 最終彙總
    print(f"\n  ── 詳細欄位最終結果 ──")
    field_line(PASS if asset_size > 0 else FAIL,    "asset_size",    f"{asset_size/1e8:.2f} 億元" if asset_size > 0 else None)
    field_line(PASS if pe_ratio > 0 else WARN,      "pe_ratio",      pe_ratio if pe_ratio else None,  "台股ETF可能為0")
    field_line(PASS if expense_ratio > 0 else WARN, "expense_ratio", f"{expense_ratio*100:.4f}%" if expense_ratio else None)

    record(ticker, "asset_size",    asset_size)
    record(ticker, "pe_ratio",      pe_ratio)
    record(ticker, "expense_ratio", expense_ratio)

    time.sleep(1)


# ══════════════════════════════════════════
#  美股診斷
# ══════════════════════════════════════════

def diagnose_us(ticker):
    header(f"美股 ETF：{ticker}")
    price = 0.0

    # ────────────────────────────────────────
    # [A] 即時報價：Yahoo v8 chart
    # ────────────────────────────────────────
    subheader("A. 即時報價（Yahoo v8 chart 10d）")
    try:
        s = new_session(f"https://finance.yahoo.com/quote/{ticker}")
        s.headers["Origin"] = "https://finance.yahoo.com"
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=10d&interval=1d"
        r = s.get(url, timeout=12)
        print(f"  {INFO} Yahoo chart HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            result = j.get("chart", {}).get("result")
            if result:
                meta   = result[0].get("meta", {})
                quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
                closes  = [c for c in (quotes.get("close") or []) if c is not None]
                highs   = [h for h in (quotes.get("high")  or []) if h is not None]
                lows    = [l for l in (quotes.get("low")   or []) if l is not None]
                volumes = [v for v in (quotes.get("volume") or []) if v is not None]

                meta_price = safe_float(meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or 0)
                prev_close = safe_float(meta.get("chartPreviousClose") or meta.get("previousClose") or 0)
                w52h = safe_float(meta.get("fiftyTwoWeekHigh") or 0)
                w52l = safe_float(meta.get("fiftyTwoWeekLow")  or 0)

                if len(closes) >= 2:
                    price = safe_float(closes[-1])
                    prev  = safe_float(closes[-2])
                elif meta_price > 0:
                    price = meta_price
                    prev  = prev_close if prev_close > 0 else meta_price
                else:
                    price = 0.0; prev = 0.0

                chg   = round(price - prev, 4) if prev > 0 else 0
                chg_p = round(chg / prev * 100, 4) if prev > 0 else 0

                field_line(PASS if price > 0 else FAIL, "current_price",        price)
                field_line(PASS if prev > 0  else FAIL, "prev_close",           prev)
                field_line(PASS, "price_change",          chg)
                field_line(PASS, "price_change_percent",  f"{chg_p}%")
                field_line(PASS if highs else WARN,  "day_high",  safe_float(highs[-1])  if highs else None)
                field_line(PASS if lows else WARN,   "day_low",   safe_float(lows[-1])   if lows  else None)
                field_line(PASS if volumes else WARN, "volume",   int(volumes[-1])        if volumes else None)
                field_line(PASS if w52h > 0 else WARN, "fifty_two_week_high", w52h, "來自 meta")
                field_line(PASS if w52l > 0 else WARN, "fifty_two_week_low",  w52l, "來自 meta")

                record_ok(ticker, "current_price", price)
                record_ok(ticker, "price_change", chg)
                record_ok(ticker, "price_change_percent", chg_p)
                record_ok(ticker, "day_high", safe_float(highs[-1]) if highs else 0)
                record_ok(ticker, "day_low",  safe_float(lows[-1])  if lows  else 0)
                record_ok(ticker, "volume",   int(volumes[-1])       if volumes else 0)
                if w52h > 0: record_ok(ticker, "fifty_two_week_high", w52h)
                if w52l > 0: record_ok(ticker, "fifty_two_week_low",  w52l)
            else:
                field_line(FAIL, "current_price", None, "result 為空")
                record_fail(ticker, "current_price", "result 為空")
        elif r.status_code == 429:
            field_line(FAIL, "current_price", None, "429 Too Many Requests")
            record_fail(ticker, "current_price", "429")
        else:
            field_line(FAIL, "current_price", None, f"HTTP {r.status_code}")
            record_fail(ticker, "current_price", f"HTTP {r.status_code}")
    except Exception as e:
        field_line(FAIL, "current_price", None, str(e))
        record_fail(ticker, "current_price", str(e))

    time.sleep(1)

    # ────────────────────────────────────────
    # [B] 配息：Yahoo v8 chart events=dividends
    # ────────────────────────────────────────
    subheader("B. 配息（Yahoo v8 chart events=dividends）")
    try:
        s = new_session(f"https://finance.yahoo.com/quote/{ticker}")
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=2y&interval=1mo&events=dividends"
        r = s.get(url, timeout=12)
        print(f"  {INFO} Yahoo dividends HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            result = j.get("chart", {}).get("result")
            if result:
                events = result[0].get("events", {}).get("dividends", {})
                cutoff = time.time() - 365 * 86400
                recent = [v["amount"] for v in events.values()
                          if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
                n = len(recent)
                total_div = sum(recent)
                div_yield = round(total_div / price * 100, 4) if price > 0 and total_div > 0 else 0
                freq = ("月配" if n >= 10 else "季配" if n >= 3 else "半年配" if n == 2
                        else "年配" if n == 1 else "不配息")

                field_line(PASS if div_yield > 0 else WARN, "dividend_yield",
                           f"{div_yield}%", f"近1年{n}次，合計=${total_div:.4f}")
                field_line(PASS if n > 0 else WARN, "payout_freq", freq)

                record_ok(ticker, "dividend_yield", div_yield)
                record_ok(ticker, "payout_freq", freq)

                if events:
                    sorted_ev = sorted(events.values(), key=lambda x: x["date"])[-4:]
                    print(f"  {INFO} 最近配息：")
                    for ev in sorted_ev:
                        dt = datetime.datetime.fromtimestamp(ev["date"]).strftime("%Y-%m-%d")
                        print(f"       {dt}  ${ev['amount']:.4f}")
            else:
                field_line(FAIL, "dividend_yield", None, "result 為空")
                record_fail(ticker, "dividend_yield")
        else:
            field_line(FAIL, "dividend_yield", None, f"HTTP {r.status_code}")
            record_fail(ticker, "dividend_yield", f"HTTP {r.status_code}")
    except Exception as e:
        field_line(FAIL, "dividend_yield", None, str(e))
        record_fail(ticker, "dividend_yield", str(e))

    time.sleep(1)

    # ────────────────────────────────────────
    # [C] 歷史月線 → 年化報酬率
    # ────────────────────────────────────────
    subheader("C. 歷史月線 → 年化報酬率（Yahoo v8 chart 5y 月線）")
    closes_5y = []
    try:
        s = new_session(f"https://finance.yahoo.com/quote/{ticker}")
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1mo"
        r = s.get(url, timeout=12)
        print(f"  {INFO} Yahoo 月線 HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            result = j.get("chart", {}).get("result")
            if result:
                quotes    = result[0].get("indicators", {}).get("quote", [{}])[0]
                closes_5y = [safe_float(c) for c in (quotes.get("close") or []) if c is not None]
                n = len(closes_5y)
                field_line(PASS if n >= 12 else WARN, "歷史月線資料筆數", n)

                r1y = annualized_return(closes_5y[-12:] if n >= 12 else closes_5y, 1)
                r3y = annualized_return(closes_5y[-36:] if n >= 36 else closes_5y, 3)
                r5y = annualized_return(closes_5y, 5)

                field_line(PASS if r1y is not None else WARN, "annual_return_1y", f"{r1y}%")
                field_line(PASS if r3y is not None else WARN, "annual_return_3y", f"{r3y}%")
                field_line(PASS if r5y is not None else WARN, "annual_return_5y", f"{r5y}%")
                field_line(INFO, "最新5筆月線收盤", f"${closes_5y[-1]:.2f}" if closes_5y else "無")

                record_ok(ticker, "annual_return_1y", r1y)
                record_ok(ticker, "annual_return_3y", r3y)
                record_ok(ticker, "annual_return_5y", r5y)
            else:
                field_line(FAIL, "月線歷史", None, "result 為空")
    except Exception as e:
        field_line(FAIL, "月線歷史", None, str(e))

    time.sleep(1)

    # ────────────────────────────────────────
    # [D] 詳細資料：Yahoo quoteSummary
    # ────────────────────────────────────────
    subheader("D. 詳細資料（Yahoo quoteSummary）")
    asset_size    = 0.0
    pe_ratio      = 0.0
    expense_ratio = 0.0
    try:
        s = new_session(f"https://finance.yahoo.com/quote/{ticker}")
        url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
               f"?modules=summaryDetail,defaultKeyStatistics,fundProfile,topHoldings")
        r = s.get(url, timeout=12)
        print(f"  {INFO} quoteSummary HTTP {r.status_code}")
        if r.status_code == 200:
            j  = r.json()
            qs = j.get("quoteSummary", {}).get("result", [{}])
            raw = qs[0] if qs else {}

            # 展平 raw 值
            merged = {}
            for section in raw.values():
                if isinstance(section, dict):
                    for k, v in section.items():
                        if isinstance(v, dict) and "raw" in v:
                            merged[k] = v["raw"]
                        elif not isinstance(v, (dict, list)):
                            merged[k] = v

            # asset_size
            for k in ("totalAssets", "netAssets"):
                v = merged.get(k)
                if v and safe_float(v) > 0:
                    asset_size = safe_float(v)
                    field_line(PASS, f"asset_size [{k}]", f"${asset_size/1e9:.2f}B")
                    break

            # PE
            for k in ("trailingPE", "forwardPE"):
                v = merged.get(k)
                if v and safe_float(v) > 0:
                    pe_ratio = safe_float(v)
                    field_line(PASS, f"pe_ratio [{k}]", pe_ratio)
                    break

            # expense_ratio（從 fundProfile.feesExpensesInvestment）
            fees = raw.get("fundProfile", {}).get("feesExpensesInvestment", {})
            for k in ("annualReportExpenseRatioNet", "annualReportExpenseRatio", "netExpenseRatio"):
                v = fees.get(k)
                if isinstance(v, dict) and "raw" in v:
                    val = safe_float(v["raw"])
                    if val > 0:
                        expense_ratio = val
                        field_line(PASS, f"expense_ratio [fees.{k}]", f"{expense_ratio*100:.4f}%")
                        break
                elif v and safe_float(v) > 0:
                    expense_ratio = safe_float(v)
                    field_line(PASS, f"expense_ratio [fees.{k}]", f"{expense_ratio*100:.4f}%")
                    break

            # 如果 fees 沒拿到，從 merged 試
            if not expense_ratio:
                for k in ("annualReportExpenseRatio", "expenseRatio"):
                    v = merged.get(k)
                    if v and safe_float(v) > 0:
                        expense_ratio = safe_float(v)
                        field_line(PASS, f"expense_ratio (merged) [{k}]", f"{expense_ratio*100:.4f}%")
                        break

            # 52週高低（quoteSummary 備援）
            w52h = safe_float(merged.get("fiftyTwoWeekHigh") or 0)
            w52l = safe_float(merged.get("fiftyTwoWeekLow")  or 0)
            if w52h > 0 and ticker not in results.get(ticker, {}).get("fifty_two_week_high", (None, None, None)):
                field_line(PASS if w52h > 0 else WARN, "fifty_two_week_high (quoteSummary)", w52h)

            field_line(PASS if asset_size    > 0 else FAIL, "asset_size    最終", f"${asset_size/1e9:.2f}B" if asset_size else None)
            field_line(PASS if pe_ratio      > 0 else WARN, "pe_ratio      最終", pe_ratio if pe_ratio else None, "部分ETF無PE")
            field_line(PASS if expense_ratio > 0 else FAIL, "expense_ratio 最終", f"{expense_ratio*100:.4f}%" if expense_ratio else None)

        else:
            field_line(FAIL, "quoteSummary", None, f"HTTP {r.status_code}")
    except Exception as e:
        field_line(FAIL, "quoteSummary", None, str(e))

    record(ticker, "asset_size",    asset_size)
    record(ticker, "pe_ratio",      pe_ratio)
    record(ticker, "expense_ratio", expense_ratio)

    time.sleep(1)


# ══════════════════════════════════════════
#  總結報告
# ══════════════════════════════════════════

def print_summary():
    header("📊  診斷總結報告")

    # 欄位重要性分類
    critical_fields = [
        "current_price", "price_change", "price_change_percent",
        "volume", "day_high", "day_low",
        "dividend_yield", "payout_freq",
        "annual_return_1y", "asset_size",
    ]
    important_fields = [
        "annual_return_3y", "annual_return_5y",
        "expense_ratio", "pe_ratio",
        "fifty_two_week_high", "fifty_two_week_low",
    ]

    all_pass = True
    for ticker, fields in results.items():
        print(f"\n  【{ticker}】")
        has_fail = False
        for field, (status, value, note) in fields.items():
            importance = ""
            if field in critical_fields:
                importance = " ★"
            elif field in important_fields:
                importance = " ☆"
            val_str = str(value) if value is not None else "(無)"
            note_str = f"  {note}" if note else ""
            print(f"    {status} {field:<32}{importance:<2}  {val_str}{note_str}")
            if status == FAIL:
                has_fail = True
                all_pass = False

        if has_fail:
            print(f"    {WARN} 此代碼有欄位抓取失敗，請確認來源是否被封鎖")
        else:
            print(f"    {PASS} 所有欄位抓取正常")

    print(f"\n{'='*65}")
    if all_pass:
        print(f"  {PASS}  全部代碼診斷通過！")
    else:
        print(f"  {WARN}  有欄位未能正常抓取，請參考上方說明。")
        print(f"       ★ = 核心欄位  ☆ = 重要欄位")
    print('='*65)


# ══════════════════════════════════════════
#  主程式
# ══════════════════════════════════════════

if __name__ == "__main__":
    print("="*65)
    print("  ETF 系統全欄位診斷腳本 v2")
    print(f"  台股：{', '.join(TW_TICKERS)}")
    print(f"  美股：{', '.join(US_TICKERS)}")
    print(f"  時間：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*65)

    for ticker in TW_TICKERS:
        diagnose_tw(ticker.strip())
        time.sleep(2)

    for ticker in US_TICKERS:
        diagnose_us(ticker.strip())
        time.sleep(2)

    print_summary()
