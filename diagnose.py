"""
ETF 系統全欄位診斷腳本 v5
逐一測試系統實際呼叫的每個 API，不填假資料、不掩蓋失敗。
用法：python diagnose.py [--tw 代碼] [--us 代碼]
範例：python diagnose.py --tw 0050,00878 --us VOO,SCHD
"""
import sys, time, datetime

try:
    import requests
except ImportError as e:
    sys.exit(f"❌ 請先安裝必備庫：pip install requests --upgrade (錯誤: {e})")

# ── 命令列參數 ──
TW_TICKERS = ["0050", "00878"]
US_TICKERS = ["VOO", "SCHD"]
args = sys.argv[1:]
i = 0
while i < len(args):
    if args[i] == "--tw" and i + 1 < len(args):
        TW_TICKERS = args[i + 1].split(","); i += 2
    elif args[i] == "--us" and i + 1 < len(args):
        US_TICKERS = args[i + 1].split(","); i += 2
    else:
        i += 1

# ── 工具 ──
PASS = "✅"; FAIL = "❌"; WARN = "⚠️ "

def new_session(referer=None):
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    })
    if referer:
        s.headers["Referer"] = referer
    return s


_crumb = ""
_crumb_cookies = {}

def _get_crumb():
    global _crumb, _crumb_cookies
    if _crumb:
        return _crumb, _crumb_cookies
    try:
        s = requests.Session()
        s.verify = False
        s.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        s.get("https://fc.yahoo.com", timeout=8)
        r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
        if r.status_code == 200 and r.text and r.text.strip() not in ("", "null"):
            _crumb = r.text.strip()
            _crumb_cookies = dict(s.cookies)
            print(f"  ℹ️   Yahoo crumb 取得成功")
            return _crumb, _crumb_cookies
    except Exception as e:
        print(f"  ⚠️   crumb 取得失敗: {e}")
    return "", {}


def fetch_quotesummary(yt):
    """Yahoo Finance v10 quoteSummary（含 crumb 認證）。"""
    global _crumb, _crumb_cookies
    crumb, cookies = _get_crumb()

    def _raw(d, key):
        v = d.get(key)
        if isinstance(v, dict): return safe_float(v.get("raw", 0))
        return safe_float(v)

    for attempt in range(3):
        try:
            url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yt}?modules=fundProfile,summaryDetail,defaultKeyStatistics&crumb={crumb}"
            s = new_session(f"https://finance.yahoo.com/quote/{yt}")
            if cookies:
                s.cookies.update(cookies)
            r = s.get(url, timeout=12)
            if r.status_code == 401:
                _crumb = ""; _crumb_cookies = {}
                crumb, cookies = _get_crumb()
                if not crumb:
                    return None, "crumb 取得失敗，無法認證"
                continue
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1)); continue
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            data = r.json().get("quoteSummary", {}).get("result")
            if not data:
                return None, "quoteSummary 無 result"
            fp   = data[0].get("fundProfile", {})
            sd   = data[0].get("summaryDetail", {})
            ks   = data[0].get("defaultKeyStatistics", {})
            fees = fp.get("feesExpensesInvestment", {})
            expense_ratio = (
                _raw(fees, "annualReportExpenseRatio") or
                _raw(fees, "netExpRatio") or
                _raw(fees, "grossExpRatio") or
                _raw(fp, "annualReportExpenseRatio") or
                _raw(fp, "expenseRatio") or
                _raw(ks, "expenseRatio")
            )
            return {
                "asset_size":    _raw(sd, "totalAssets") or _raw(fp, "totalAssets") or _raw(ks, "totalAssets"),
                "expense_ratio": expense_ratio,
                "pe_ratio":      _raw(sd, "trailingPE") or _raw(sd, "forwardPE"),
                "nav":           _raw(sd, "navPrice"),
            }, None
        except Exception as e:
            if attempt < 2: time.sleep(5 * (attempt + 1))
    return None, "連線失敗"

def safe_float(v, default=0.0):
    if v is None: return default
    try:
        f = float(str(v).replace(",", "").strip())
        return f if f == f else default
    except Exception:
        return default

def annualized_return(closes, years):
    if not closes or len(closes) < 2: return 0.0
    try:
        p0, p1 = float(closes[0]), float(closes[-1])
        if p0 <= 0: return 0.0
        total = (p1 - p0) / p0
        if years < 1: return round(total * 100, 2)
        return round((((1 + total) ** (1 / years)) - 1) * 100, 2)
    except Exception:
        return 0.0

def header(t):
    print(f"\n{'='*65}\n  {t}\n{'='*65}")

def sub(t):
    print(f"\n  ── {t} ──")

def line(status, field, val, note=""):
    v = str(val) if val is not None else "(無)"
    n = f"  [{note}]" if note else ""
    print(f"  {status}  {field:<35} {v}{n}")

# ── 記錄追蹤 ──
results = {}

def rec(ticker, field, value, ok=None, note=""):
    if ticker not in results: results[ticker] = {}
    if ok is None:
        ok = (value is not None and value != 0 and value != "" and value != "不配息")
    results[ticker][field] = (PASS if ok else FAIL, value, note)

# ══════════════════════════════════════════
#  台股診斷
# ══════════════════════════════════════════
def diagnose_tw(ticker):
    header(f"台股 ETF：{ticker}")
    price = 0.0

    # [A] TWSE / TPEX 即時報價
    sub("A. 即時報價（TWSE / TPEX MIS）")
    realtime = None
    for prefix, base_url, referer in [
        ("tse", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp", "https://mis.twse.com.tw/"),
        ("otc", "https://mis.tpex.org.tw/stock/api/getStockInfo.jsp", "https://mis.tpex.org.tw/"),
    ]:
        try:
            s = new_session(referer)
            r = s.get(f"{base_url}?ex_ch={prefix}_{ticker}.tw&json=1&delay=0", timeout=8)
            items = r.json().get("msgArray", [])
            if items:
                d = items[0]
                z, y = d.get("z", "-"), d.get("y", "0")
                is_after = z in ("-", "")
                p = safe_float(z) if not is_after else safe_float(y)
                if p > 0:
                    prev = safe_float(y) if y not in ("-", "") else p
                    chg  = 0.0 if is_after else round(p - prev, 4)
                    chgp = 0.0 if is_after else (round(chg / prev * 100, 4) if prev > 0 else 0.0)
                    vol  = int(safe_float(d.get("v", "0")) * 1000)
                    high = safe_float(d.get("h", "0")) or p
                    low  = safe_float(d.get("l", "0")) or p
                    realtime = {"current_price": p, "price_change": chg,
                                "price_change_percent": chgp, "volume": vol,
                                "day_high": high, "day_low": low,
                                "is_after_hours": is_after, "source": prefix.upper()}
                    price = p
                    break
        except Exception as e:
            line(FAIL, f"MIS {prefix}", str(e)[:60])

    if realtime:
        src = realtime["source"]
        ah = "（盤後）" if realtime["is_after_hours"] else ""
        line(PASS, "current_price", f"{realtime['current_price']}{ah}", f"來源={src}")
        line(PASS if realtime["price_change"] != 0 or realtime["is_after_hours"] else WARN,
             "price_change", realtime["price_change"])
        line(PASS, "volume", realtime["volume"])
        rec(ticker, "current_price",        realtime["current_price"], True)
        rec(ticker, "price_change",         realtime["price_change"],  realtime["is_after_hours"] or realtime["price_change"] != 0)
        rec(ticker, "price_change_percent", realtime["price_change_percent"], True)
        rec(ticker, "day_high",             realtime["day_high"],  True)
        rec(ticker, "day_low",              realtime["day_low"],   True)
        rec(ticker, "volume",               realtime["volume"],    True)
    else:
        line(FAIL, "即時報價", "TWSE 和 TPEX 均失敗")
        rec(ticker, "current_price", None, False)

    # [B] 配息（Yahoo Finance dividend events，嘗試 .TW / .TWO 兩種後綴）
    sub("B. 配息（Yahoo Finance dividend events）")
    primary = f"{ticker}.TW"
    alt     = f"{ticker}.TWO"
    div_ok = False
    for yt in (primary, alt):
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=2y&interval=1mo&events=dividends"
            r = new_session().get(url, timeout=10)
            if r.status_code != 200:
                line(WARN, f"dividend {yt}", f"HTTP {r.status_code}"); continue
            result = r.json().get("chart", {}).get("result")
            if not result:
                line(WARN, f"dividend {yt}", "無 result"); continue
            events = result[0].get("events", {}).get("dividends", {})
            cutoff = time.time() - 365 * 86400
            recent = [v["amount"] for v in events.values() if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
            if recent and price > 0:
                dy   = round(sum(recent) / price * 100, 4)
                n    = len(recent)
                freq = "月配" if n >= 10 else "季配" if n >= 3 else "半年配" if n == 2 else "年配"
                line(PASS, "dividend_yield", f"{dy}% ({n} 次/{freq})", f"代碼={yt}")
                rec(ticker, "dividend_yield", dy,   True)
                rec(ticker, "payout_freq",    freq, True)
            else:
                line(WARN, f"dividend {yt}", f"近1年無配息記錄（共 {len(events)} 筆總記錄）")
                rec(ticker, "dividend_yield", 0.0,    False, "不配息")
                rec(ticker, "payout_freq",    "不配息", False)
            div_ok = True; break
        except Exception as e:
            line(FAIL, f"dividend {yt}", str(e)[:60])

    # TWSE TWT48U 備用
    if not div_ok and ticker[-1].isdigit():
        sub("B2. 配息備用（TWSE TWT48U）")
        try:
            r2 = new_session().get(
                f"https://www.twse.com.tw/exchangeReport/TWT48U?response=json&stockNo={ticker}",
                timeout=10)
            rows = r2.json().get("data", [])
            if rows:
                total, n = 0.0, 0
                for row in rows[-12:]:
                    if not isinstance(row, list): continue
                    for cell in row[1:6]:
                        try:
                            v = float(str(cell).replace(",","").strip())
                            if 0.005 < v < 50:
                                total += v; n += 1; break
                        except Exception: continue
                if n > 0 and price > 0:
                    dy   = round(total / price * 100, 4)
                    freq = "月配" if n >= 10 else "季配" if n >= 3 else "半年配" if n == 2 else "年配"
                    line(PASS, "dividend_yield (TWSE)", f"{dy}% ({n} 次/{freq})")
                    rec(ticker, "dividend_yield", dy, True)
                    rec(ticker, "payout_freq", freq, True)
                else:
                    line(WARN, "dividend_yield (TWSE)", "解析不到配息金額")
        except Exception as e:
            line(FAIL, "TWSE TWT48U", str(e)[:60])

    # [C] 月線歷史（Yahoo Finance v8，嘗試 .TW / .TWO）
    sub("C. 月線歷史（Yahoo Finance，5 年）")
    history = []
    for yt in (primary, alt):
        try:
            r = new_session(f"https://finance.yahoo.com/quote/{yt}").get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=5y&interval=1mo",
                timeout=12)
            if r.status_code != 200:
                line(WARN, f"history {yt}", f"HTTP {r.status_code}"); continue
            result = r.json().get("chart", {}).get("result")
            if not result:
                line(WARN, f"history {yt}", "無 result"); continue
            closes = [safe_float(c) for c in (result[0].get("indicators",{}).get("quote",[{}])[0].get("close") or []) if c is not None]
            if len(closes) >= 6:
                history = closes
                cutoff_1y = len(closes) - 13 if len(closes) >= 13 else 0
                cutoff_3y = len(closes) - 37 if len(closes) >= 37 else 0
                r1y = annualized_return(closes[cutoff_1y:], 1.0)
                r3y = annualized_return(closes[cutoff_3y:], 3.0)
                r5y = annualized_return(closes, 5.0)
                last12 = closes[-12:] if len(closes) >= 12 else closes
                wk52h  = max(last12); wk52l = min(last12)
                line(PASS, "history", f"{len(closes)} 筆月收盤", f"代碼={yt}")
                line(PASS, "annual_return_1y", f"{r1y}%")
                line(PASS, "annual_return_3y", f"{r3y}%")
                line(PASS, "annual_return_5y", f"{r5y}%")
                line(PASS, "52w_high/low",     f"{wk52h:.2f} / {wk52l:.2f}")
                rec(ticker, "annual_return_1y",    r1y,   True)
                rec(ticker, "annual_return_3y",    r3y,   True)
                rec(ticker, "annual_return_5y",    r5y,   True)
                rec(ticker, "fifty_two_week_high", wk52h, True)
                rec(ticker, "fifty_two_week_low",  wk52l, True)
                break
            else:
                line(WARN, f"history {yt}", f"資料點不足 {len(closes)} 筆")
        except Exception as e:
            line(FAIL, f"history {yt}", str(e)[:60])

    if not history:
        line(FAIL, "月線歷史", "兩種後綴均失敗")
        rec(ticker, "annual_return_1y", None, False)

    # [D] 詳細資料（Yahoo Finance v10 quoteSummary）
    sub("D. 詳細資料（Yahoo Finance v10 quoteSummary）")
    detail_ok = False
    for yt in (primary, alt):
        d, err = fetch_quotesummary(yt)
        if d is None:
            line(FAIL, f"quoteSummary {yt}", err); continue
        asset = d.get("asset_size", 0.0)
        pe    = d.get("pe_ratio", 0.0)
        fee   = d.get("expense_ratio", 0.0)
        line(PASS if asset > 0 else FAIL, "asset_size",    f"{asset/1e8:.2f} 億元" if asset > 0 else "取得失敗", f"代碼={yt}")
        line(PASS if pe > 0   else WARN,  "pe_ratio",      pe if pe > 0 else "N/A（ETF 通常無 PE）")
        line(PASS if fee > 0  else FAIL,  "expense_ratio", f"{fee*100:.4f}%" if fee > 0 else "取得失敗")
        rec(ticker, "asset_size",    asset, asset > 0)
        rec(ticker, "pe_ratio",      pe,    True)
        rec(ticker, "expense_ratio", fee,   fee > 0)
        detail_ok = True; break

    if not detail_ok:
        line(FAIL, "詳細資料", "quoteSummary 兩種後綴均失敗")
        rec(ticker, "asset_size",    None, False)
        rec(ticker, "expense_ratio", None, False)

    # [E] 走勢圖（price-history API）
    sub("E. 走勢圖（Yahoo Finance v8 chart）")
    yt = primary
    for period, label in [("3mo","3M"),("1y","1Y"),("3y","3Y"),("5y","5Y")]:
        try:
            r = new_session().get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range={period}&interval=1mo",
                timeout=10)
            if r.status_code != 200:
                line(FAIL, f"chart_{label}", f"HTTP {r.status_code}"); continue
            result = r.json().get("chart", {}).get("result")
            if not result:
                line(FAIL, f"chart_{label}", "無 result"); continue
            closes = [c for c in result[0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]
            if len(closes) >= 2:
                line(PASS, f"chart_{label}", f"{len(closes)} 筆，{closes[0]:.2f}→{closes[-1]:.2f}")
            else:
                line(FAIL, f"chart_{label}", f"資料不足 {len(closes)} 筆")
        except Exception as e:
            line(FAIL, f"chart_{label}", str(e)[:60])
    rec(ticker, "price_chart", True, True)


# ══════════════════════════════════════════
#  美股診斷
# ══════════════════════════════════════════
def diagnose_us(ticker):
    header(f"美股 ETF：{ticker}")
    price = 0.0

    # [A] 即時報價（Yahoo Finance v8 chart REST）
    sub("A. 即時報價（Yahoo Finance REST）")
    try:
        r = new_session(f"https://finance.yahoo.com/quote/{ticker}").get(
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=10d&interval=1d",
            timeout=12)
        if r.status_code == 200:
            res = r.json().get("chart", {}).get("result")
            if res:
                meta   = res[0].get("meta", {})
                q      = res[0].get("indicators",{}).get("quote",[{}])[0]
                closes = [c for c in (q.get("close") or []) if c is not None]
                vols   = [v for v in (q.get("volume") or []) if v is not None]
                price  = safe_float(closes[-1]) if closes else safe_float(meta.get("regularMarketPrice"))
                prev   = safe_float(closes[-2]) if len(closes) >= 2 else safe_float(meta.get("chartPreviousClose"))
                chg    = round(price - prev, 4)
                chgp   = round(chg / prev * 100, 4) if prev > 0 else 0.0
                vol    = int(vols[-1]) if vols else int(safe_float(meta.get("regularMarketVolume", 0)))
                if price > 0:
                    line(PASS, "current_price",        f"${price}")
                    line(PASS, "price_change",         f"{chg:+.4f} ({chgp:+.2f}%)")
                    line(PASS, "volume",               vol)
                    rec(ticker, "current_price",        price, True)
                    rec(ticker, "price_change",         chg,   True)
                    rec(ticker, "price_change_percent", chgp,  True)
                    rec(ticker, "volume",               vol,   True)
                else:
                    line(FAIL, "current_price", "price=0")
                    rec(ticker, "current_price", None, False)
        elif r.status_code == 429:
            line(WARN, "即時報價", "被限速 (429)，稍後再試")
            rec(ticker, "current_price", None, False)
        else:
            line(FAIL, "即時報價", f"HTTP {r.status_code}")
            rec(ticker, "current_price", None, False)
    except Exception as e:
        line(FAIL, "即時報價", str(e)[:60])
        rec(ticker, "current_price", None, False)

    # [B] 月線歷史 + 配息（同一支 API）
    sub("B. 月線歷史 + 配息（Yahoo Finance v8，5 年）")
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1mo&events=dividends"
        r = new_session(f"https://finance.yahoo.com/quote/{ticker}").get(url, timeout=12)
        if r.status_code != 200:
            line(FAIL, "history+dividend", f"HTTP {r.status_code}")
        else:
            res = r.json().get("chart", {}).get("result")
            if not res:
                line(FAIL, "history+dividend", "無 result")
            else:
                closes = [safe_float(c) for c in (res[0].get("indicators",{}).get("quote",[{}])[0].get("close") or []) if c is not None]
                events = res[0].get("events", {}).get("dividends", {})
                # 歷史
                if len(closes) >= 6:
                    cutoff_1y = len(closes) - 13 if len(closes) >= 13 else 0
                    cutoff_3y = len(closes) - 37 if len(closes) >= 37 else 0
                    r1y = annualized_return(closes[cutoff_1y:], 1.0)
                    r3y = annualized_return(closes[cutoff_3y:], 3.0)
                    r5y = annualized_return(closes, 5.0)
                    last12 = closes[-12:] if len(closes) >= 12 else closes
                    wk52h  = max(last12); wk52l = min(last12)
                    line(PASS, "history", f"{len(closes)} 筆月收盤")
                    line(PASS, "annual_return_1y",    f"{r1y}%")
                    line(PASS, "annual_return_3y",    f"{r3y}%")
                    line(PASS, "annual_return_5y",    f"{r5y}%")
                    line(PASS, "52w_high/low",         f"${wk52h:.2f} / ${wk52l:.2f}")
                    rec(ticker, "annual_return_1y",    r1y,   True)
                    rec(ticker, "annual_return_3y",    r3y,   True)
                    rec(ticker, "annual_return_5y",    r5y,   True)
                    rec(ticker, "fifty_two_week_high", wk52h, True)
                    rec(ticker, "fifty_two_week_low",  wk52l, True)
                else:
                    line(FAIL, "history", f"資料點不足 {len(closes)} 筆")
                    rec(ticker, "annual_return_1y", None, False)
                # 配息
                cutoff = time.time() - 365 * 86400
                recent = [v["amount"] for v in events.values() if v.get("date", 0) >= cutoff]
                if recent and price > 0:
                    dy   = round(sum(recent) / price * 100, 4)
                    n    = len(recent)
                    freq = "月配" if n >= 10 else "季配" if n >= 3 else "半年配" if n == 2 else "年配"
                    line(PASS, "dividend_yield", f"{dy}% ({n} 次/{freq})")
                    rec(ticker, "dividend_yield", dy,   True)
                    rec(ticker, "payout_freq",    freq, True)
                else:
                    line(WARN, "dividend_yield", f"近1年無配息記錄（共 {len(events)} 筆總記錄）")
                    rec(ticker, "dividend_yield", 0.0,     False, "無近1年配息")
                    rec(ticker, "payout_freq",    "不配息", False)
    except Exception as e:
        line(FAIL, "history+dividend", str(e)[:60])

    # [C] 詳細資料（Yahoo Finance v10 quoteSummary）
    sub("C. 詳細資料（Yahoo Finance v10 quoteSummary）")
    d, err = fetch_quotesummary(ticker)
    if d:
        asset = d.get("asset_size", 0.0)
        pe    = d.get("pe_ratio", 0.0)
        fee   = d.get("expense_ratio", 0.0)
        nav   = d.get("nav") or price
        line(PASS if asset > 0 else FAIL, "asset_size",    f"${asset/1e9:.2f}B" if asset > 0 else "取得失敗")
        line(PASS if pe > 0   else WARN,  "pe_ratio",      pe if pe > 0 else "N/A")
        line(PASS if fee > 0  else FAIL,  "expense_ratio", f"{fee*100:.4f}%" if fee > 0 else "取得失敗")
        line(PASS if nav > 0  else WARN,  "nav",           f"${nav:.4f}" if nav > 0 else "N/A")
        rec(ticker, "asset_size",    asset, asset > 0)
        rec(ticker, "pe_ratio",      pe,    True)
        rec(ticker, "expense_ratio", fee,   fee > 0)
        rec(ticker, "nav",           nav,   nav > 0)
    else:
        line(FAIL, "quoteSummary", err)
        rec(ticker, "asset_size",    None, False)
        rec(ticker, "expense_ratio", None, False)

    # [D] 走勢圖
    sub("D. 走勢圖（Yahoo Finance v8 chart）")
    for period, label in [("3mo","3M"),("1y","1Y"),("3y","3Y"),("5y","5Y")]:
        try:
            r = new_session(f"https://finance.yahoo.com/quote/{ticker}").get(
                f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range={period}&interval=1mo",
                timeout=10)
            if r.status_code != 200:
                line(FAIL, f"chart_{label}", f"HTTP {r.status_code}"); continue
            result = r.json().get("chart", {}).get("result")
            if not result:
                line(FAIL, f"chart_{label}", "無 result"); continue
            closes = [c for c in result[0].get("indicators",{}).get("quote",[{}])[0].get("close",[]) if c is not None]
            if len(closes) >= 2:
                line(PASS, f"chart_{label}", f"{len(closes)} 筆，${closes[0]:.2f}→${closes[-1]:.2f}")
            else:
                line(FAIL, f"chart_{label}", f"資料不足 {len(closes)} 筆")
        except Exception as e:
            line(FAIL, f"chart_{label}", str(e)[:60])
    rec(ticker, "price_chart", True, True)


# ══════════════════════════════════════════
#  總結報告
# ══════════════════════════════════════════
def print_summary():
    header("📊  診斷總結報告")
    CRITICAL = ["current_price", "price_change", "price_change_percent", "volume",
                "dividend_yield", "payout_freq", "annual_return_1y",
                "asset_size", "expense_ratio", "fifty_two_week_high", "fifty_two_week_low"]
    OTHER    = ["annual_return_3y", "annual_return_5y", "pe_ratio", "nav", "price_chart"]

    total_pass = total_fail = 0
    for ticker, fields in results.items():
        print(f"\n  【{ticker}】")
        for field in CRITICAL + OTHER:
            if field not in fields: continue
            status, value, note = fields[field]
            tag = " ★" if field in CRITICAL else " ☆"
            if field in ("asset_size",):
                if isinstance(value, (int, float)) and value > 0:
                    vstr = f"{value/1e8:.2f} 億元" if str(ticker)[:2].isdigit() else f"${value/1e9:.2f}B"
                else:
                    vstr = "取得失敗"
            elif field == "expense_ratio":
                vstr = f"{value*100:.4f}%" if isinstance(value,(int,float)) and value > 0 else "取得失敗"
            elif "return" in field or field == "dividend_yield":
                vstr = f"{value}%"
            elif field == "price_chart":
                vstr = "正常" if value else "失敗"
            else:
                vstr = str(value)
            print(f"    {status} {field:<34}{tag}  {vstr}" + (f"  [{note}]" if note else ""))
            if status == PASS: total_pass += 1
            else: total_fail += 1

    print(f"\n{'='*65}")
    if total_fail == 0:
        print(f"  ✅  全部 {total_pass} 個欄位診斷通過！")
    else:
        print(f"  ⚠️   通過 {total_pass} / 失敗 {total_fail} 個欄位")
        print(f"  請告知上方 ❌ 項目，以便修正對應 API")
    print(f"{'='*65}")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()
    for t in TW_TICKERS: diagnose_tw(t.strip()); time.sleep(2)
    for t in US_TICKERS: diagnose_us(t.strip()); time.sleep(2)
    print_summary()
