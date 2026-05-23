"""
ETF 系統全欄位診斷腳本 v7
對齊目前程式架構，完整偵測所有欄位與全部 8 個走勢圖期間：
  1D / 5D / 1M / 6M / YTD / 1Y / 5Y / All

變更 (v7)：
  - expense_ratio 加入 KNOWN_EXPENSE_RATIO 靜態備援（Yahoo v10 對台股常失敗）
  - payout_freq 改用 best_freq()：取 Yahoo 偵測 與 靜態備援 中頻率等級較高者
  - KNOWN_PAYOUT_FREQ 同步更新（0050 → 半年配）
  - 修正 utcfromtimestamp DeprecationWarning → fromtimestamp(tz=UTC)

用法：
  python diagnose.py                          # 預設測 0050, 00878, VOO, SCHD
  python diagnose.py --tw 0050,00919 --us SPY,JEPI
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

# ── 工具函式 ──
PASS = "✅"; FAIL = "❌"; WARN = "⚠️ "

def new_session(referer=None):
    s = requests.Session()
    s.verify = False
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
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
        s = new_session()
        s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        s.get("https://fc.yahoo.com", timeout=8)
        r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
        if r.status_code == 200 and r.text and r.text.strip() not in ("", "null"):
            _crumb = r.text.strip()
            _crumb_cookies = dict(s.cookies)
            print(f"  ℹ️   Yahoo crumb 取得成功：{_crumb[:12]}…")
            return _crumb, _crumb_cookies
    except Exception as e:
        print(f"  ⚠️   crumb 取得失敗：{e}")
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
            url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yt}"
                   f"?modules=fundProfile,summaryDetail,defaultKeyStatistics&crumb={crumb}")
            s = new_session(f"https://finance.yahoo.com/quote/{yt}")
            if cookies:
                s.cookies.update(cookies)
            r = s.get(url, timeout=12)
            if r.status_code == 401:
                _crumb = ""; _crumb_cookies = {}
                crumb, cookies = _get_crumb()
                if not crumb: return None, "crumb 取得失敗"
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
                _raw(fees, "annualReportExpenseRatio") or _raw(fees, "netExpRatio") or
                _raw(fees, "grossExpRatio") or _raw(fp, "annualReportExpenseRatio") or
                _raw(fp, "expenseRatio") or _raw(ks, "expenseRatio")
            )
            return {
                "asset_size":    _raw(sd, "totalAssets") or _raw(fp, "totalAssets") or _raw(ks, "totalAssets"),
                "expense_ratio": expense_ratio,
                "pe_ratio":      _raw(sd, "trailingPE") or _raw(sd, "forwardPE"),
                "nav":           _raw(sd, "navPrice"),
                "div_yield":     (_raw(sd, "yield") or _raw(sd, "dividendYield")) * 100,
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
        return round(((1 + total) ** (1 / years) - 1) * 100, 2)
    except Exception:
        return 0.0


def classify_freq(n: int) -> str:
    """與主程式 _classify_freq 完全一致的頻率分類：支援全部 5 種配息頻率。"""
    if n >= 9: return "月配"
    if n >= 5: return "雙月配"
    if n >= 3: return "季配"
    if n == 2: return "半年配"
    if n == 1: return "年配"
    return "不配息"


# ── 頻率等級（數字越大頻率越高） ──
_FREQ_RANK = {'不配息': 0, '年配': 1, '半年配': 2, '季配': 3, '雙月配': 4, '月配': 5}

KNOWN_PAYOUT_FREQ = {
    '0050':'半年配','006208':'半年配','0056':'季配','00878':'季配',
    '00919':'月配','00929':'月配','00713':'季配','00940':'月配',
    '00891':'季配','00692':'年配','0051':'年配','0052':'年配',
    '0053':'年配','00850':'季配','00757':'季配','00939':'月配',
    '00934':'月配','00936':'月配','00944':'月配','00900':'季配',
    '00907':'季配','00915':'雙月配','00892':'季配','00861':'季配',
    '00679B':'半年配','00687B':'半年配','00695B':'半年配','00720B':'季配',
    '006205':'年配',
    'SPY':'季配','QQQ':'季配','VOO':'季配','VTI':'季配','SCHD':'季配',
    'IVV':'季配','VYM':'季配','JEPI':'月配','SOXL':'季配','ARKK':'不配息',
    'IWM':'季配','DIA':'月配','VIG':'季配','XLK':'季配','SMH':'季配',
    'SOXX':'季配','VGT':'季配','TLT':'月配','IEF':'月配','AGG':'月配',
    'BND':'月配','GLD':'不配息','VNQ':'季配','VEA':'季配','VWO':'季配',
    'EEM':'半年配','XLF':'季配','XLE':'季配','XLV':'季配',
}

KNOWN_EXPENSE_RATIO = {
    '0050':0.0046,'006208':0.0046,'0056':0.0066,'00878':0.0065,
    '00919':0.0090,'00929':0.0095,'00713':0.0045,'00940':0.0065,
    '00891':0.0075,'00692':0.0061,'0051':0.0044,'0052':0.0053,
    '0053':0.0044,'00850':0.0060,'00757':0.0099,'00939':0.0080,
    '00934':0.0085,'00936':0.0085,'00944':0.0080,'00900':0.0090,
    '00907':0.0075,'00915':0.0080,'00892':0.0065,'00861':0.0080,
    '00679B':0.0015,'00687B':0.0017,'00695B':0.0020,'00720B':0.0025,
    '006205':0.0099,
    'SPY':0.0009,'QQQ':0.0020,'VOO':0.0003,'VTI':0.0003,'SCHD':0.0006,
    'IVV':0.0003,'VYM':0.0006,'JEPI':0.0035,'SOXL':0.0176,'ARKK':0.0075,
    'IWM':0.0019,'DIA':0.0016,'VIG':0.0006,'XLK':0.0009,'SMH':0.0035,
    'SOXX':0.0035,'VGT':0.0010,'TLT':0.0015,'IEF':0.0015,'AGG':0.0003,
    'BND':0.0003,'GLD':0.0040,'VNQ':0.0013,'VEA':0.0005,'VWO':0.0008,
    'EEM':0.0068,'XLF':0.0009,'XLE':0.0009,'XLV':0.0009,
}


def best_freq(yf_count: int, ticker: str) -> str:
    """取 Yahoo 事件數 與 靜態備援 兩者中頻率等級較高者（防漏抓或偵測升頻）。"""
    yf_f    = classify_freq(yf_count)
    known_f = KNOWN_PAYOUT_FREQ.get(ticker, '')
    if not known_f:
        return yf_f
    return known_f if _FREQ_RANK.get(known_f, 0) >= _FREQ_RANK.get(yf_f, 0) else yf_f


def header(t):
    print(f"\n{'='*70}\n  {t}\n{'='*70}")

def sub(t):
    print(f"\n  ── {t} ──")

def line(status, field, val, note=""):
    v = str(val) if val is not None else "(無)"
    n = f"  [{note}]" if note else ""
    print(f"  {status}  {field:<38} {v}{n}")

# ── 記錄追蹤 ──
results = {}

def rec(ticker, field, value, ok=None, note=""):
    if ticker not in results: results[ticker] = {}
    if ok is None:
        ok = (value is not None and value != 0 and value != "" and value != "不配息")
    results[ticker][field] = (PASS if ok else FAIL, value, note)


# ══════════════════════════════════════════════════════════
#  走勢圖期間設定（對齊目前程式 etf_routes.py）
# ══════════════════════════════════════════════════════════
# 格式：(期間代號, 顯示名稱, Yahoo range, Yahoo interval, 是否盤中資料)
CHART_PERIODS = [
    ("1D",  "1D",  "1d",   "5m",   True),   # 盤中 5 分鐘
    ("5D",  "5D",  "5d",   "15m",  True),   # 近 5 日 15 分鐘
    ("1M",  "1M",  "1mo",  "1d",   False),  # 近 1 個月日線
    ("6M",  "6M",  "6mo",  "1d",   False),  # 近 6 個月日線
    ("YTD", "YTD", "ytd",  "1d",   False),  # 今年至今日線
    ("1Y",  "1Y",  "1y",   "1d",   False),  # 近 1 年日線
    ("5Y",  "5Y",  "5y",   "1wk",  False),  # 近 5 年週線
    ("ALL", "All", "max",  "1mo",  False),  # 全部歷史月線
]


def check_chart_period(symbol, period_id, period_label, yf_range, yf_interval, is_intraday,
                        tz_offset_h=8):
    """測試單一走勢圖期間，回傳 (ok, msg)。"""
    try:
        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?range={yf_range}&interval={yf_interval}")
        r = new_session(f"https://finance.yahoo.com/quote/{symbol}").get(url, timeout=12)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        result = r.json().get("chart", {}).get("result")
        if not result:
            return False, "無 result"
        ts     = result[0].get("timestamp", [])
        closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
        pairs  = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if len(pairs) < 2:
            return False, f"有效資料點僅 {len(pairs)} 筆"

        # 計算時間標籤範圍
        t_first, p_first = pairs[0]
        t_last,  p_last  = pairs[-1]
        _UTC = datetime.timezone.utc
        if is_intraday:
            fmt = "%H:%M" if period_id == "1D" else "%m/%d %H:%M"
            dt_first = (datetime.datetime.fromtimestamp(t_first, tz=_UTC) +
                        datetime.timedelta(hours=tz_offset_h)).strftime(fmt)
            dt_last  = (datetime.datetime.fromtimestamp(t_last,  tz=_UTC) +
                        datetime.timedelta(hours=tz_offset_h)).strftime(fmt)
            lbl = f"{dt_first}→{dt_last}"
        else:
            dt_first = datetime.datetime.fromtimestamp(t_first, tz=_UTC).strftime("%Y-%m-%d")
            dt_last  = datetime.datetime.fromtimestamp(t_last,  tz=_UTC).strftime("%Y-%m-%d")
            lbl = f"{dt_first}→{dt_last}"

        return True, f"{len(pairs)} 筆  {lbl}  {p_first:.2f}→{p_last:.2f}"
    except Exception as e:
        return False, str(e)[:70]


# ══════════════════════════════════════════════════════════
#  台股診斷
# ══════════════════════════════════════════════════════════
def diagnose_tw(ticker):
    header(f"台股 ETF：{ticker}")
    price = 0.0

    # [A] 即時報價（TWSE / TPEX MIS）
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
                    realtime = {
                        "current_price": p, "price_change": chg,
                        "price_change_percent": chgp, "volume": vol,
                        "day_high": high, "day_low": low,
                        "is_after_hours": is_after, "source": prefix.upper(),
                    }
                    price = p
                    break
        except Exception as e:
            line(FAIL, f"MIS {prefix}", str(e)[:60])

    if realtime:
        src = realtime["source"]
        ah  = "（盤後）" if realtime["is_after_hours"] else ""
        line(PASS, "current_price",        f"NT${realtime['current_price']}{ah}", f"來源={src}")
        line(PASS if realtime["price_change"] != 0 or realtime["is_after_hours"] else WARN,
             "price_change",               realtime["price_change"])
        line(PASS, "price_change_percent", f"{realtime['price_change_percent']}%")
        line(PASS, "volume",               realtime["volume"])
        line(PASS, "day_high / day_low",   f"{realtime['day_high']} / {realtime['day_low']}")
        rec(ticker, "current_price",        realtime["current_price"], True)
        rec(ticker, "price_change",         realtime["price_change"],
            realtime["is_after_hours"] or realtime["price_change"] != 0)
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
    div_ok  = False
    for yt in (primary, alt):
        try:
            url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}"
                   f"?range=2y&interval=1mo&events=dividends")
            r = new_session().get(url, timeout=10)
            if r.status_code != 200:
                line(WARN, f"dividend {yt}", f"HTTP {r.status_code}"); continue
            result = r.json().get("chart", {}).get("result")
            if not result:
                line(WARN, f"dividend {yt}", "無 result"); continue
            events = result[0].get("events", {}).get("dividends", {})
            cutoff = time.time() - 365 * 86400
            recent = [v["amount"] for v in events.values()
                      if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
            if recent and price > 0:
                dy     = round(sum(recent) / price * 100, 4)
                n      = len(recent)
                yf_f   = classify_freq(n)
                bf     = best_freq(n, ticker)
                note   = (f"代碼={yt}" if yf_f == bf
                          else f"代碼={yt}  Yahoo推算={yf_f}→靜態備援校正={bf}")
                line(PASS, "dividend_yield",
                     f"{dy}%（近12月 {n} 次 → {bf}）", note)
                rec(ticker, "dividend_yield", dy,  True)
                rec(ticker, "payout_freq",    bf,  bf != "不配息")
            else:
                known_f = KNOWN_PAYOUT_FREQ.get(ticker, "")
                line(WARN, f"dividend {yt}",
                     f"近1年無配息記錄（歷史共 {len(events)} 筆）",
                     f"靜態備援頻率={known_f}" if known_f else "無靜態備援")
                rec(ticker, "dividend_yield", 0.0,     False, "近1年無配息")
                rec(ticker, "payout_freq", known_f or "不配息", bool(known_f), "KNOWN_PAYOUT_FREQ")
            div_ok = True; break
        except Exception as e:
            line(FAIL, f"dividend {yt}", str(e)[:60])

    # TWSE TWT48U 備用（每筆 = 一個年度彙總）
    if not div_ok and ticker[-1].isdigit():
        sub("B2. 配息備用（TWSE TWT48U 年度彙總）")
        try:
            r2 = new_session().get(
                f"https://www.twse.com.tw/exchangeReport/TWT48U?response=json&stockNo={ticker}",
                timeout=10)
            rows = r2.json().get("data", []) if r2.status_code == 200 else []
            if rows:
                # 每筆 = 一年度，column[1] = 現金股利合計（NOT 個別次數）
                # 取最近一筆有效金額做殖利率；頻率由靜態備援決定
                for row in reversed(rows):
                    if not isinstance(row, list) or len(row) < 2: continue
                    try:
                        div_amt = float(str(row[1]).replace(",", "").strip())
                        if 0.01 < div_amt < 200:
                            dy = round(div_amt / price * 100, 4) if price > 0 else 0.0
                            line(PASS, "dividend_yield (TWT48U 年度彙總)",
                                 f"{dy}%  年配息額={div_amt:.2f}",
                                 "頻率由 KNOWN_PAYOUT_FREQ 決定")
                            rec(ticker, "dividend_yield", dy, dy > 0)
                            rec(ticker, "payout_freq", "靜態備援補", True, "KNOWN_PAYOUT_FREQ")
                            break
                    except (ValueError, TypeError):
                        continue
            else:
                line(FAIL, "TWSE TWT48U", "無資料或 HTTP 錯誤")
        except Exception as e:
            line(FAIL, "TWSE TWT48U", str(e)[:60])

    # [C] 月線歷史（Yahoo Finance v8，5 年，嘗試 .TW / .TWO）
    sub("C. 月線歷史（Yahoo Finance 5 年月線）")
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
            closes = [safe_float(c) for c in
                      (result[0].get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                      if c is not None]
            if len(closes) >= 6:
                history = closes
                c1y = len(closes) - 13 if len(closes) >= 13 else 0
                c3y = len(closes) - 37 if len(closes) >= 37 else 0
                r1y = annualized_return(closes[c1y:], 1.0)
                r3y = annualized_return(closes[c3y:], 3.0)
                r5y = annualized_return(closes, 5.0)
                last12 = closes[-12:] if len(closes) >= 12 else closes
                wk52h  = max(last12); wk52l = min(last12)
                line(PASS, "月線資料點",      f"{len(closes)} 筆", f"代碼={yt}")
                line(PASS, "annual_return_1y", f"{r1y:+.2f}%")
                line(PASS, "annual_return_3y", f"{r3y:+.2f}%")
                line(PASS, "annual_return_5y", f"{r5y:+.2f}%")
                line(PASS, "52週高/低",        f"NT${wk52h:.2f} / NT${wk52l:.2f}")
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
        # 靜態備援：Yahoo v10 對台股常不提供 expense_ratio
        fee_src = "Yahoo"
        if not fee:
            fee     = KNOWN_EXPENSE_RATIO.get(ticker, 0.0)
            fee_src = "靜態備援" if fee else ""
        line(PASS if asset > 0 else FAIL,
             "asset_size",    f"NT${asset/1e8:.2f} 億" if asset > 0 else "取得失敗", f"代碼={yt}")
        line(PASS if pe  > 0 else WARN, "pe_ratio",      pe if pe > 0 else "N/A（ETF 通常無 PE）")
        line(PASS if fee > 0 else FAIL, "expense_ratio",
             f"{fee*100:.4f}%  [{fee_src}]" if fee > 0 else "取得失敗（Yahoo+靜態備援均無）")
        rec(ticker, "asset_size",    asset, asset > 0)
        rec(ticker, "pe_ratio",      pe,    True)
        rec(ticker, "expense_ratio", fee,   fee > 0)
        detail_ok = True; break

    if not detail_ok:
        # quoteSummary 完全失敗時嘗試靜態備援
        fee_static = KNOWN_EXPENSE_RATIO.get(ticker, 0.0)
        line(FAIL, "詳細資料", "quoteSummary 兩種後綴均失敗")
        if fee_static:
            line(WARN, "expense_ratio", f"{fee_static*100:.4f}%  [靜態備援]",
                 "quoteSummary 失敗但靜態備援補上")
        rec(ticker, "asset_size",    None, False)
        rec(ticker, "expense_ratio", fee_static, fee_static > 0)

    # [E] 走勢圖 — 全部 8 個期間（對齊 etf_routes.py RANGE_MAP / INTERVAL_MAP）
    sub("E. 價格走勢圖（全部 8 個期間 × Yahoo Finance v8 chart）")
    yt_chart = primary   # 先用 .TW，若全失敗再 .TWO
    chart_pass = chart_fail = 0
    for pid, plabel, yf_range, yf_interval, intraday in CHART_PERIODS:
        ok, msg = check_chart_period(yt_chart, pid, plabel, yf_range, yf_interval,
                                     intraday, tz_offset_h=8)
        # 如果 .TW 失敗，嘗試 .TWO
        if not ok:
            ok2, msg2 = check_chart_period(f"{ticker}.TWO", pid, plabel, yf_range, yf_interval,
                                           intraday, tz_offset_h=8)
            if ok2:
                ok, msg = ok2, msg2 + "  [.TWO]"
        status = PASS if ok else FAIL
        line(status, f"chart_{plabel:<5}  ({yf_range:>3}/{yf_interval:<4})", msg)
        rec(ticker, f"chart_{plabel}", ok, ok)
        if ok: chart_pass += 1
        else:  chart_fail += 1

    summary_status = PASS if chart_fail == 0 else (WARN if chart_pass > 0 else FAIL)
    line(summary_status, "走勢圖總計",
         f"{chart_pass}/8 期間通過" + ("" if chart_fail == 0 else f"，{chart_fail} 個失敗"))

    print()


# ══════════════════════════════════════════════════════════
#  美股診斷
# ══════════════════════════════════════════════════════════
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
                q      = res[0].get("indicators", {}).get("quote", [{}])[0]
                closes = [c for c in (q.get("close")  or []) if c is not None]
                highs  = [h for h in (q.get("high")   or []) if h is not None]
                lows   = [l for l in (q.get("low")    or []) if l is not None]
                vols   = [v for v in (q.get("volume") or []) if v is not None]
                price  = safe_float(closes[-1]) if closes else safe_float(meta.get("regularMarketPrice"))
                prev   = safe_float(closes[-2]) if len(closes) >= 2 else safe_float(meta.get("chartPreviousClose"))
                chg    = round(price - prev, 4)
                chgp   = round(chg / prev * 100, 4) if prev > 0 else 0.0
                vol    = int(vols[-1]) if vols else int(safe_float(meta.get("regularMarketVolume", 0)))
                dh     = safe_float(highs[-1]) if highs else price
                dl     = safe_float(lows[-1])  if lows  else price
                if price > 0:
                    line(PASS, "current_price",        f"${price}")
                    line(PASS, "price_change",         f"{chg:+.4f} ({chgp:+.2f}%)")
                    line(PASS, "day_high / day_low",   f"${dh:.2f} / ${dl:.2f}")
                    line(PASS, "volume",               vol)
                    rec(ticker, "current_price",        price, True)
                    rec(ticker, "price_change",         chg,   True)
                    rec(ticker, "price_change_percent", chgp,  True)
                    rec(ticker, "day_high",             dh,    True)
                    rec(ticker, "day_low",              dl,    True)
                    rec(ticker, "volume",               vol,   True)
                else:
                    line(FAIL, "current_price", "price=0"); rec(ticker, "current_price", None, False)
        elif r.status_code == 429:
            line(WARN, "即時報價", "被限速 (429)"); rec(ticker, "current_price", None, False)
        else:
            line(FAIL, "即時報價", f"HTTP {r.status_code}"); rec(ticker, "current_price", None, False)
    except Exception as e:
        line(FAIL, "即時報價", str(e)[:60]); rec(ticker, "current_price", None, False)

    # [B] 月線歷史 + 配息（同一支 API，5 年月線）
    sub("B. 月線歷史 + 配息（Yahoo Finance v8，5 年）")
    try:
        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
               f"?range=5y&interval=1mo&events=dividends")
        r = new_session(f"https://finance.yahoo.com/quote/{ticker}").get(url, timeout=12)
        if r.status_code != 200:
            line(FAIL, "history+dividend", f"HTTP {r.status_code}")
        else:
            res = r.json().get("chart", {}).get("result")
            if not res:
                line(FAIL, "history+dividend", "無 result")
            else:
                closes = [safe_float(c) for c in
                          (res[0].get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                          if c is not None]
                events = res[0].get("events", {}).get("dividends", {})
                # 歷史
                if len(closes) >= 6:
                    c1y = len(closes) - 13 if len(closes) >= 13 else 0
                    c3y = len(closes) - 37 if len(closes) >= 37 else 0
                    r1y = annualized_return(closes[c1y:], 1.0)
                    r3y = annualized_return(closes[c3y:], 3.0)
                    r5y = annualized_return(closes, 5.0)
                    last12 = closes[-12:] if len(closes) >= 12 else closes
                    wk52h  = max(last12); wk52l = min(last12)
                    line(PASS, "月線資料點",      f"{len(closes)} 筆")
                    line(PASS, "annual_return_1y", f"{r1y:+.2f}%")
                    line(PASS, "annual_return_3y", f"{r3y:+.2f}%")
                    line(PASS, "annual_return_5y", f"{r5y:+.2f}%")
                    line(PASS, "52週高/低",        f"${wk52h:.2f} / ${wk52l:.2f}")
                    rec(ticker, "annual_return_1y",    r1y,   True)
                    rec(ticker, "annual_return_3y",    r3y,   True)
                    rec(ticker, "annual_return_5y",    r5y,   True)
                    rec(ticker, "fifty_two_week_high", wk52h, True)
                    rec(ticker, "fifty_two_week_low",  wk52l, True)
                else:
                    line(FAIL, "月線歷史", f"資料點不足 {len(closes)} 筆")
                    rec(ticker, "annual_return_1y", None, False)
                # 配息
                cutoff = time.time() - 365 * 86400
                recent = [v["amount"] for v in events.values() if v.get("date", 0) >= cutoff]
                if recent and price > 0:
                    dy     = round(sum(recent) / price * 100, 4)
                    n      = len(recent)
                    yf_f   = classify_freq(n)
                    bf     = best_freq(n, ticker)
                    note   = ("" if yf_f == bf
                              else f"Yahoo推算={yf_f}→靜態備援校正={bf}")
                    line(PASS, "dividend_yield",
                         f"{dy}%（近12月 {n} 次 → {bf}）", note)
                    rec(ticker, "dividend_yield", dy,  True)
                    rec(ticker, "payout_freq",    bf,  bf != "不配息")
                else:
                    known_f = KNOWN_PAYOUT_FREQ.get(ticker, "")
                    line(WARN, "dividend_yield",
                         f"近1年無配息記錄（歷史共 {len(events)} 筆）",
                         f"靜態備援頻率={known_f}" if known_f else "無靜態備援")
                    rec(ticker, "dividend_yield", 0.0,     False, "近1年無配息")
                    rec(ticker, "payout_freq", known_f or "不配息", bool(known_f), "KNOWN_PAYOUT_FREQ")
    except Exception as e:
        line(FAIL, "history+dividend", str(e)[:60])

    # [C] 詳細資料（Yahoo Finance v10 quoteSummary）
    sub("C. 詳細資料（Yahoo Finance v10 quoteSummary）")
    d, err = fetch_quotesummary(ticker)
    if d:
        asset  = d.get("asset_size", 0.0)
        pe     = d.get("pe_ratio", 0.0)
        fee    = d.get("expense_ratio", 0.0)
        nav    = d.get("nav") or price
        yf_yld = d.get("div_yield", 0.0)
        # 靜態備援：若 Yahoo 未提供 expense_ratio
        fee_src = "Yahoo"
        if not fee:
            fee     = KNOWN_EXPENSE_RATIO.get(ticker, 0.0)
            fee_src = "靜態備援" if fee else ""
        line(PASS if asset > 0 else FAIL, "asset_size",
             f"${asset/1e9:.2f}B" if asset > 0 else "取得失敗")
        line(PASS if pe  > 0 else WARN,  "pe_ratio",      pe if pe > 0 else "N/A")
        line(PASS if fee > 0 else FAIL,  "expense_ratio",
             f"{fee*100:.4f}%  [{fee_src}]" if fee > 0 else "取得失敗（Yahoo+靜態備援均無）")
        line(PASS if nav > 0 else WARN,  "nav",           f"${nav:.4f}" if nav > 0 else "N/A")
        line(PASS if yf_yld > 0 else WARN, "yf_div_yield",
             f"{yf_yld:.4f}%" if yf_yld > 0 else "N/A（quoteSummary 補充值）")
        rec(ticker, "asset_size",    asset,  asset > 0)
        rec(ticker, "pe_ratio",      pe,     True)
        rec(ticker, "expense_ratio", fee,    fee > 0)
        rec(ticker, "nav",           nav,    nav > 0)
    else:
        fee_static = KNOWN_EXPENSE_RATIO.get(ticker, 0.0)
        line(FAIL, "quoteSummary", err)
        if fee_static:
            line(WARN, "expense_ratio", f"{fee_static*100:.4f}%  [靜態備援]",
                 "quoteSummary 失敗但靜態備援補上")
        rec(ticker, "asset_size",    None, False)
        rec(ticker, "expense_ratio", fee_static, fee_static > 0)

    # [D] 走勢圖 — 全部 8 個期間（對齊 etf_routes.py）
    sub("D. 價格走勢圖（全部 8 個期間 × Yahoo Finance v8 chart）")
    chart_pass = chart_fail = 0
    for pid, plabel, yf_range, yf_interval, intraday in CHART_PERIODS:
        ok, msg = check_chart_period(ticker, pid, plabel, yf_range, yf_interval,
                                     intraday, tz_offset_h=-4)  # US: EDT = UTC-4
        status = PASS if ok else FAIL
        line(status, f"chart_{plabel:<5}  ({yf_range:>3}/{yf_interval:<4})", msg)
        rec(ticker, f"chart_{plabel}", ok, ok)
        if ok: chart_pass += 1
        else:  chart_fail += 1

    summary_status = PASS if chart_fail == 0 else (WARN if chart_pass > 0 else FAIL)
    line(summary_status, "走勢圖總計",
         f"{chart_pass}/8 期間通過" + ("" if chart_fail == 0 else f"，{chart_fail} 個失敗"))

    print()


# ══════════════════════════════════════════════════════════
#  總結報告
# ══════════════════════════════════════════════════════════
CHART_PERIOD_KEYS = [f"chart_{p[1]}" for p in CHART_PERIODS]

CRITICAL_FIELDS = [
    "current_price", "price_change", "price_change_percent",
    "day_high", "day_low", "volume",
    "dividend_yield", "payout_freq",
    "annual_return_1y", "annual_return_3y", "annual_return_5y",
    "asset_size", "expense_ratio",
    "fifty_two_week_high", "fifty_two_week_low",
]
OTHER_FIELDS = ["pe_ratio", "nav"] + CHART_PERIOD_KEYS


def print_summary():
    header("📊  診斷總結報告")
    total_pass = total_fail = 0

    for ticker, fields in results.items():
        print(f"\n  【{ticker}】")
        all_fields = CRITICAL_FIELDS + OTHER_FIELDS
        for field in all_fields:
            if field not in fields: continue
            status, value, note = fields[field]
            tag = " ★" if field in CRITICAL_FIELDS else " ☆"

            if field == "asset_size":
                if isinstance(value, (int, float)) and value > 0:
                    vstr = (f"NT${value/1e8:.2f}億" if str(ticker)[:2].isdigit()
                            else f"${value/1e9:.2f}B")
                else:
                    vstr = "取得失敗"
            elif field == "expense_ratio":
                vstr = f"{value*100:.4f}%" if isinstance(value, (int, float)) and value > 0 else "取得失敗"
            elif "return" in field or field == "dividend_yield":
                vstr = f"{value:+.2f}%" if isinstance(value, (int, float)) else str(value)
            elif field.startswith("chart_"):
                vstr = "✓ 有資料" if value else "✗ 無資料"
            else:
                vstr = str(value)

            suffix = f"  [{note}]" if note else ""
            print(f"    {status} {field:<36}{tag}  {vstr}{suffix}")
            if status == PASS: total_pass += 1
            else: total_fail += 1

    print(f"\n{'='*70}")
    if total_fail == 0:
        print(f"  ✅  全部 {total_pass} 個欄位診斷通過！")
    else:
        pct = total_pass / (total_pass + total_fail) * 100
        print(f"  ⚠️   通過 {total_pass} / 失敗 {total_fail}  ({pct:.0f}%)")
        print(f"  請將上方 ❌ 項目截圖回報，以便定向修正對應 API")
    print(f"{'='*70}")


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings()

    print(f"\n🔍 ETF 系統診斷腳本 v6  —  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   台股：{TW_TICKERS}    美股：{US_TICKERS}")
    print(f"   走勢圖期間：{[p[1] for p in CHART_PERIODS]}")

    for t in TW_TICKERS:
        diagnose_tw(t.strip())
        time.sleep(2)
    for t in US_TICKERS:
        diagnose_us(t.strip())
        time.sleep(2)

    print_summary()
