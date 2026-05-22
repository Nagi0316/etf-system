"""
etf_data.py — ETF 靜態清單、資料抓取、DB 存取
（從原 main.py 提取並整合 exchange_rate）
"""
from __future__ import annotations
import random, time, logging
from datetime import datetime, date
from typing import Optional

import requests as req_lib
import certifi
import yfinance as yf
from dateutil.relativedelta import relativedelta

from cache import cache, CACHE_TTL_DETAIL
from database import get_db
from utils import safe_float

logger = logging.getLogger(__name__)

# ── UA 池：模擬多種真實瀏覽器，避免固定特徵被封鎖 ──
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

def _new_session(referer: str = "") -> req_lib.Session:
    """建立模擬真實瀏覽器的 Session，隨機 UA + 完整 headers，降低被封鎖機率。"""
    ua = random.choice(_UA_POOL)
    is_firefox = "Firefox" in ua
    s = req_lib.Session()
    s.verify = certifi.where()
    s.headers.update({
        "User-Agent": ua,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,*/*;q=0.8") if is_firefox else
                  ("text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
                   "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"),
        "Accept-Language": random.choice([
            "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "zh-TW,zh;q=0.8,en;q=0.6",
            "en-US,en;q=0.9,zh-TW;q=0.8",
        ]),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    if referer:
        s.headers["Referer"] = referer
    return s


def _jitter(base: float = 1.0, spread: float = 2.0):
    """在下一次請求前加入隨機延遲，模擬人工瀏覽行為，避免固定節奏被偵測。"""
    time.sleep(base + random.uniform(0, spread))


def _get_with_retry(session: req_lib.Session, url: str, timeout: int = 12,
                    max_attempts: int = 3) -> Optional[req_lib.Response]:
    """帶指數退避的 GET，自動處理 429 限速與瞬斷重試。"""
    for attempt in range(max_attempts):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = 30 * (2 ** attempt) + random.uniform(0, 10)
                logger.debug(f"429 rate-limited {url[:60]}, wait {wait:.1f}s")
                time.sleep(wait)
                continue
            if r.status_code in (403, 401):
                logger.debug(f"HTTP {r.status_code} {url[:60]}")
                return r   # 回傳讓呼叫方決定處理方式
            logger.debug(f"HTTP {r.status_code} {url[:60]}")
            return None
        except req_lib.exceptions.Timeout:
            logger.debug(f"Timeout {url[:60]} attempt {attempt+1}")
        except req_lib.exceptions.ConnectionError as e:
            logger.debug(f"ConnectionError {url[:60]}: {e}")
        if attempt < max_attempts - 1:
            time.sleep(5 * (attempt + 1) + random.uniform(0, 3))
    return None


# ── Yahoo Finance crumb 快取（v10 API 認證用） ──
_yf_crumb: str = ""
_yf_crumb_cookies: dict = {}

def _refresh_yahoo_crumb() -> bool:
    global _yf_crumb, _yf_crumb_cookies
    for attempt in range(3):
        try:
            s = _new_session()
            s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            r1 = s.get("https://fc.yahoo.com", timeout=8)
            if r1.status_code not in (200, 302, 303):
                time.sleep(5); continue
            r2 = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
            if r2.status_code == 200 and r2.text and r2.text.strip() not in ("", "null"):
                _yf_crumb = r2.text.strip()
                _yf_crumb_cookies = dict(s.cookies)
                logger.debug("Yahoo crumb 取得成功")
                return True
        except Exception as e:
            logger.debug(f"crumb refresh attempt {attempt+1}: {e}")
        time.sleep(10 * (attempt + 1))
    return False


def _yahoo_ticker(ticker: str, market: str) -> str:
    if market == "TW":
        return f"{ticker}.TWO" if ticker.upper().endswith("B") else f"{ticker}.TW"
    return ticker


def _annualized_return(closes: list, years: float) -> float:
    if not closes or len(closes) < 5:
        return 0.0
    try:
        p0, p1 = float(closes[0]), float(closes[-1])
        if p0 <= 0:
            return 0.0
        total = (p1 - p0) / p0
        if years < 1:
            return round(total * 100, 2)
        return round(((1 + total) ** (1 / years) - 1) * 100, 2)
    except Exception as e:
        logger.debug(f"annualized_return calc: {e}")
        return 0.0


# ══════════════════════════════════════════════════════════
#  ETF 靜態清單
# ══════════════════════════════════════════════════════════

TW_ETFS = [
    {'ticker': '0050',   'name': '元大台灣50',          'market': 'TW', 'hot': True},
    {'ticker': '006208', 'name': '富邦台50',             'market': 'TW', 'hot': True},
    {'ticker': '0056',   'name': '元大高股息',            'market': 'TW', 'hot': True},
    {'ticker': '00878',  'name': '國泰永續高股息',         'market': 'TW', 'hot': True},
    {'ticker': '00919',  'name': '群益台灣精選高息',       'market': 'TW', 'hot': True},
    {'ticker': '00929',  'name': '復華台灣科技優息',       'market': 'TW', 'hot': True},
    {'ticker': '00713',  'name': '元大台灣高息低波',       'market': 'TW', 'hot': True},
    {'ticker': '00940',  'name': '元大台灣價值高息',       'market': 'TW', 'hot': True},
    {'ticker': '00891',  'name': '中信關鍵半導體',         'market': 'TW', 'hot': True},
    {'ticker': '00692',  'name': '富邦公司治理',           'market': 'TW', 'hot': True},
    {'ticker': '0051',   'name': '元大中型100',           'market': 'TW'},
    {'ticker': '0052',   'name': '富邦科技',              'market': 'TW'},
    {'ticker': '0053',   'name': '元大電子',              'market': 'TW'},
    {'ticker': '00692',  'name': '富邦公司治理',           'market': 'TW'},
    {'ticker': '00850',  'name': '元大臺灣ESG永續',        'market': 'TW'},
    {'ticker': '00757',  'name': '統一FANG+',             'market': 'TW'},
    {'ticker': '00929',  'name': '復華台灣科技優息',       'market': 'TW'},
    {'ticker': '00713',  'name': '元大台灣高息低波',       'market': 'TW'},
    {'ticker': '00940',  'name': '元大台灣價值高息',       'market': 'TW'},
    {'ticker': '00939',  'name': '統一台灣高息動能',       'market': 'TW'},
    {'ticker': '00934',  'name': '中信成長高股息',         'market': 'TW'},
    {'ticker': '00936',  'name': '台新台灣永續高息',       'market': 'TW'},
    {'ticker': '00944',  'name': '群益半導體收益',         'market': 'TW'},
    {'ticker': '00900',  'name': '富邦特選高股息30',       'market': 'TW'},
    {'ticker': '00907',  'name': '永豐優息存股',           'market': 'TW'},
    {'ticker': '00915',  'name': '凱基優選高股息30',       'market': 'TW'},
    {'ticker': '00891',  'name': '中信關鍵半導體',         'market': 'TW'},
    {'ticker': '00892',  'name': '富邦台灣半導體',         'market': 'TW'},
    {'ticker': '00861',  'name': '元大全球AI',             'market': 'TW'},
    {'ticker': '00679B', 'name': '元大美債20年',           'market': 'TW'},
    {'ticker': '00687B', 'name': '國泰20年美債',           'market': 'TW'},
    {'ticker': '00695B', 'name': '富邦美債20年',           'market': 'TW'},
    {'ticker': '00720B', 'name': '元大投資級公司債',       'market': 'TW'},
    {'ticker': '00679B', 'name': '元大美債20年',           'market': 'TW'},
    {'ticker': '006205', 'name': '富邦上証',               'market': 'TW'},
]

US_ETFS = [
    {'ticker': 'SPY',  'name': 'SPDR S&P 500 ETF Trust',            'market': 'US', 'hot': True},
    {'ticker': 'QQQ',  'name': 'Invesco QQQ Trust',                  'market': 'US', 'hot': True},
    {'ticker': 'VOO',  'name': 'Vanguard S&P 500 ETF',              'market': 'US', 'hot': True},
    {'ticker': 'VTI',  'name': 'Vanguard Total Stock Market ETF',   'market': 'US', 'hot': True},
    {'ticker': 'SCHD', 'name': 'Schwab U.S. Dividend Equity ETF',   'market': 'US', 'hot': True},
    {'ticker': 'IVV',  'name': 'iShares Core S&P 500 ETF',          'market': 'US', 'hot': True},
    {'ticker': 'VYM',  'name': 'Vanguard High Dividend Yield ETF',  'market': 'US', 'hot': True},
    {'ticker': 'JEPI', 'name': 'JPMorgan Equity Premium Income ETF','market': 'US', 'hot': True},
    {'ticker': 'SOXL', 'name': 'Direxion Daily Semicon Bull 3X ETF','market': 'US', 'hot': True},
    {'ticker': 'ARKK', 'name': 'ARK Innovation ETF',                 'market': 'US', 'hot': True},
    {'ticker': 'IVV',  'name': 'iShares Core S&P 500 ETF',        'market': 'US'},
    {'ticker': 'IWM',  'name': 'iShares Russell 2000 ETF',        'market': 'US'},
    {'ticker': 'DIA',  'name': 'SPDR Dow Jones Industrial Average','market': 'US'},
    {'ticker': 'VYM',  'name': 'Vanguard High Dividend Yield ETF', 'market': 'US'},
    {'ticker': 'VIG',  'name': 'Vanguard Dividend Appreciation ETF','market': 'US'},
    {'ticker': 'XLK',  'name': 'Technology Select Sector SPDR',    'market': 'US'},
    {'ticker': 'SMH',  'name': 'VanEck Semiconductor ETF',         'market': 'US'},
    {'ticker': 'SOXX', 'name': 'iShares Semiconductor ETF',        'market': 'US'},
    {'ticker': 'VGT',  'name': 'Vanguard Information Technology',  'market': 'US'},
    {'ticker': 'ARKK', 'name': 'ARK Innovation ETF',               'market': 'US'},
    {'ticker': 'TLT',  'name': 'iShares 20+ Year Treasury Bond',   'market': 'US'},
    {'ticker': 'IEF',  'name': 'iShares 7-10 Year Treasury Bond',  'market': 'US'},
    {'ticker': 'AGG',  'name': 'iShares Core US Aggregate Bond',   'market': 'US'},
    {'ticker': 'BND',  'name': 'Vanguard Total Bond Market ETF',   'market': 'US'},
    {'ticker': 'GLD',  'name': 'SPDR Gold Shares',                 'market': 'US'},
    {'ticker': 'VNQ',  'name': 'Vanguard Real Estate ETF',         'market': 'US'},
    {'ticker': 'VEA',  'name': 'Vanguard FTSE Developed Markets',  'market': 'US'},
    {'ticker': 'VWO',  'name': 'Vanguard FTSE Emerging Markets',   'market': 'US'},
    {'ticker': 'EEM',  'name': 'iShares MSCI Emerging Markets',    'market': 'US'},
    {'ticker': 'XLF',  'name': 'Financial Select Sector SPDR',     'market': 'US'},
    {'ticker': 'XLE',  'name': 'Energy Select Sector SPDR',        'market': 'US'},
    {'ticker': 'XLV',  'name': 'Health Care Select Sector SPDR',   'market': 'US'},
]

# 去重
_seen: set = set()
_deduped: list = []
for _e in TW_ETFS + US_ETFS:
    if _e['ticker'] not in _seen:
        _seen.add(_e['ticker']); _deduped.append(_e)
ALL_ETFS = _deduped
HOT_ETFS = [e for e in ALL_ETFS if e.get('hot')]


# ══════════════════════════════════════════════════════════
#  Mock 資料（排行榜啟動備援）
# ══════════════════════════════════════════════════════════

def seed_etf_master():
    """將靜態清單的 ETF 代碼與名稱寫入 etf_master，不寫入任何假價格。"""
    hot_set = {e['ticker'] for e in HOT_ETFS}
    with get_db() as (conn, cursor):
        for etf in ALL_ETFS:
            is_hot = 1 if etf['ticker'] in hot_set else 0
            cursor.execute(
                "INSERT INTO etf_master (ticker,name,market,is_hot) VALUES (%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market), "
                "is_hot=GREATEST(is_hot,VALUES(is_hot))",
                (etf['ticker'], etf['name'], etf['market'], is_hot)
            )
        conn.commit()
    logger.info(f"✅ etf_master 種子資料完成 ({len(ALL_ETFS)} 筆)")


# ══════════════════════════════════════════════════════════
#  台股即時報價
# ══════════════════════════════════════════════════════════

def _fetch_tw_realtime_perfect(ticker: str) -> Optional[dict]:
    """TWSE / TPEX 即時報價，非交易時段回傳昨收"""
    for prefix, base_url, referer in [
        ("tse", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",  "https://mis.twse.com.tw/"),
        ("otc", "https://mis.tpex.org.tw/stock/api/getStockInfo.jsp",  "https://mis.tpex.org.tw/"),
    ]:
        try:
            s = _new_session(referer)
            url = f"{base_url}?ex_ch={prefix}_{ticker}.tw&json=1&delay=0"
            r = _get_with_retry(s, url, timeout=8, max_attempts=2)
            if not r:
                continue
            items = r.json().get("msgArray", [])
            if not items:
                continue
            d = items[0]
            z_val = d.get("z", "-")
            y_val = d.get("y", "0")
            is_after_hours = z_val in ("-", "")
            price = safe_float(z_val) if not is_after_hours else safe_float(y_val)
            if price <= 0:
                continue
            prev  = safe_float(y_val) if y_val not in ("-", "") else price
            high  = safe_float(d.get("h", "0")) or price
            low   = safe_float(d.get("l", "0")) or price
            vol_k = safe_float(d.get("v", "0"))
            # 盤後時段 z 為空，盤中價 == 昨收，強制設 0 而非算出假的 0 差
            chg     = 0.0 if is_after_hours else round(price - prev, 4)
            chg_pct = 0.0 if is_after_hours else (round(chg / prev * 100, 4) if prev > 0 else 0.0)
            return {
                "current_price": price, "price_change": chg,
                "price_change_percent": chg_pct,
                "day_high": high, "day_low": low,
                "volume": int(vol_k * 1000),
                "is_after_hours": is_after_hours,
            }
        except Exception as e:
            logger.debug(f"TW realtime {prefix} {ticker}: {e}")
    return None


def _fetch_tw_dividend(ticker: str, current_price: float) -> tuple:
    """取得台股 ETF 配息資料。

    策略：
    1. Yahoo Finance dividend events（上市與上櫃通用，最穩定；自動嘗試 .TW / .TWO 兩種後綴）
    2. TWSE TWT48U 公告（僅限上市 ETF，末位為數字的代碼）
    """
    # 自動嘗試 .TW 與 .TWO 後綴，修正非 B 結尾上櫃 ETF 查錯代碼問題
    primary = _yahoo_ticker(ticker, "TW")
    alt     = f"{ticker}.TWO" if primary.endswith(".TW") else f"{ticker}.TW"

    # 1. Yahoo Finance events（適用所有台股 ETF，含上櫃/債券/槓桿/反向）
    for yt in (primary, alt):
        try:
            url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}"
                   f"?range=2y&interval=1mo&events=dividends")
            r = _get_with_retry(_new_session(f"https://finance.yahoo.com/quote/{yt}"), url, timeout=10)
            if r and r.status_code == 200:
                result = r.json().get("chart", {}).get("result")
                if result:
                    events = result[0].get("events", {}).get("dividends", {})
                    cutoff = time.time() - 365 * 86400
                    recent = [v["amount"] for v in events.values()
                              if v.get("date", 0) >= cutoff and safe_float(v.get("amount", 0)) > 0]
                    if recent and current_price > 0:
                        dy = round(sum(recent) / current_price * 100, 4)
                        n  = len(recent)
                        freq = ("月配" if n >= 10 else "季配" if n >= 3
                                else "半年配" if n == 2 else "年配")
                        return dy, freq
        except Exception as e:
            logger.debug(f"TW dividend Yahoo {yt}: {e}")
        _jitter(0.5, 1.5)

    # 2. TWSE TWT48U（只對末位為數字的上市 ETF 有效）
    if ticker[-1].isdigit():
        try:
            url = (f"https://www.twse.com.tw/exchangeReport/TWT48U"
                   f"?response=json&stockNo={ticker}")
            r = _get_with_retry(_new_session("https://www.twse.com.tw/"), url, timeout=10)
            rows = r.json().get("data", []) if r else []
            if rows:
                recent = rows[-12:]
                total_div, n = 0.0, 0
                for row in recent:
                    if not isinstance(row, list):
                        continue
                    for cell in row[1:6]:
                        s = str(cell).replace(",", "").strip()
                        try:
                            v = float(s)
                            if 0.005 < v < 50:
                                total_div += v; n += 1; break
                        except (ValueError, TypeError):
                            continue
                if n > 0 and current_price > 0:
                    dy = round(total_div / current_price * 100, 4)
                    freq = ("月配" if n >= 10 else "季配" if n >= 3
                            else "半年配" if n == 2 else "年配")
                    return dy, freq
        except Exception as e:
            logger.debug(f"TW dividend TWSE {ticker}: {e}")

    return 0.0, "不配息"


def _fetch_tw_history(ticker: str) -> list:
    """取得 5 年月線收盤價（Yahoo 優先，自動嘗試 .TW / .TWO 兩種後綴）"""
    primary = _yahoo_ticker(ticker, "TW")
    alt     = f"{ticker}.TWO" if primary.endswith(".TW") else f"{ticker}.TW"
    for yt in (primary, alt):
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=5y&interval=1mo"
            r = _get_with_retry(_new_session(f"https://finance.yahoo.com/quote/{yt}"), url, timeout=12)
            if r and r.status_code == 200:
                result = r.json().get("chart", {}).get("result")
                if result:
                    closes = [safe_float(c) for c in (result[0].get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
                    if len(closes) >= 6:
                        return closes
        except Exception as e:
            logger.debug(f"TW history Yahoo {yt}: {e}")
    return []


def _fetch_yahoo_quotesummary(yt: str) -> dict:
    """Yahoo Finance v10 quoteSummary（含 crumb 認證，自動刷新）。"""
    global _yf_crumb, _yf_crumb_cookies
    empty = {"asset_size": 0.0, "pe_ratio": 0.0, "expense_ratio": 0.0, "nav": 0.0, "div_yield": 0.0}
    if not _yf_crumb:
        _refresh_yahoo_crumb()

    def _raw(d, key):
        v = d.get(key)
        if isinstance(v, dict): return safe_float(v.get("raw", 0))
        return safe_float(v)

    for attempt in range(3):
        try:
            url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yt}"
                   f"?modules=fundProfile,summaryDetail,defaultKeyStatistics&crumb={_yf_crumb}")
            s = _new_session()
            s.headers["Referer"] = f"https://finance.yahoo.com/quote/{yt}"
            if _yf_crumb_cookies:
                s.cookies.update(_yf_crumb_cookies)
            r = s.get(url, timeout=12)
            if r.status_code == 401:
                # crumb 過期，刷新後重試
                _yf_crumb = ""
                _yf_crumb_cookies = {}
                if _refresh_yahoo_crumb():
                    continue
                return empty
            if r.status_code == 429:
                time.sleep(20 * (attempt + 1)); continue
            if r.status_code != 200:
                return empty
            data = r.json().get("quoteSummary", {}).get("result")
            if not data:
                return empty
            fp = data[0].get("fundProfile", {})
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
                "div_yield":     (_raw(sd, "yield") or _raw(sd, "dividendYield")) * 100,
            }
        except Exception as e:
            logger.debug(f"quoteSummary {yt} attempt {attempt+1}: {e}")
            if attempt < 2: time.sleep(5 * (attempt + 1))
    return empty


def _fetch_tw_detail(ticker: str) -> dict:
    """從 Yahoo Finance v10 quoteSummary 取得台股 ETF 的費用率、規模、本益比。"""
    primary = _yahoo_ticker(ticker, "TW")
    alt     = f"{ticker}.TWO" if primary.endswith(".TW") else f"{ticker}.TW"
    for yt in (primary, alt):
        d = _fetch_yahoo_quotesummary(yt)
        if d.get("asset_size") or d.get("expense_ratio"):
            return d
    return {"asset_size": 0.0, "pe_ratio": 0.0, "expense_ratio": 0.0}


# ══════════════════════════════════════════════════════════
#  美股報價
# ══════════════════════════════════════════════════════════

def _fetch_us_quote(ticker: str) -> Optional[dict]:
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=10d&interval=1d"
    referer = f"https://finance.yahoo.com/quote/{ticker}"
    try:
        s = _new_session(referer)
        s.headers["Origin"] = "https://finance.yahoo.com"
        r = _get_with_retry(s, url, timeout=12, max_attempts=3)
        if not r or r.status_code != 200:
            return None
        j = r.json()
        result = j.get("chart", {}).get("result")
        if not result:
            return None
        meta    = result[0].get("meta", {})
        q       = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes  = [c for c in (q.get("close")  or []) if c is not None]
        highs   = [h for h in (q.get("high")   or []) if h is not None]
        lows    = [l for l in (q.get("low")    or []) if l is not None]
        volumes = [v for v in (q.get("volume") or []) if v is not None]
        price = safe_float(closes[-1]) if closes else safe_float(meta.get("regularMarketPrice"))
        prev  = safe_float(closes[-2]) if len(closes) >= 2 else safe_float(meta.get("chartPreviousClose"))
        if price <= 0:
            return None
        chg     = round(price - prev, 4)
        chg_pct = round(chg / prev * 100, 4) if prev > 0 else 0.0
        return {
            "current_price": price, "price_change": chg, "price_change_percent": chg_pct,
            "day_high": safe_float(highs[-1]) if highs else price,
            "day_low":  safe_float(lows[-1])  if lows  else price,
            "volume": int(volumes[-1]) if volumes else int(safe_float(meta.get("regularMarketVolume", 0))),
        }
    except Exception as e:
        logger.debug(f"US quote {ticker}: {e}")
    return None


# ══════════════════════════════════════════════════════════
#  主抓取入口
# ══════════════════════════════════════════════════════════

def fetch_one_etf(ticker: str, market: str) -> Optional[dict]:
    if market == "TW":
        return _fetch_tw_etf(ticker)
    return _fetch_us_etf(ticker)


def _fetch_tw_etf(ticker: str) -> Optional[dict]:
    quote = _fetch_tw_realtime_perfect(ticker)
    if not quote:
        logger.warning(f"台股 {ticker} 無法取得報價")
        return None
    price = quote["current_price"]

    div_yield, payout_freq = _fetch_tw_dividend(ticker, price)
    history_closes = _fetch_tw_history(ticker)

    # 計算年化報酬需要「起點 + N 個月」共 N+1 筆，故 cutoff 用 -(N+1)
    cutoff_1y = len(history_closes) - 13 if len(history_closes) >= 13 else 0
    cutoff_3y = len(history_closes) - 37 if len(history_closes) >= 37 else 0
    ann_1y = _annualized_return(history_closes[cutoff_1y:], 1.0)
    ann_3y = _annualized_return(history_closes[cutoff_3y:], 3.0)
    ann_5y = _annualized_return(history_closes, 5.0)
    last12 = history_closes[-12:] if len(history_closes) >= 12 else history_closes
    wk52_h = max(last12) if last12 else price * 1.15
    wk52_l = min(last12) if last12 else price * 0.85

    detail = _fetch_tw_detail(ticker)

    # 盤後時段：price_change / price_change_percent 為 0，嘗試從 DB 補上最後一個交易日的值
    price_change     = quote["price_change"]
    price_change_pct = quote["price_change_percent"]
    if quote.get("is_after_hours") and price_change == 0:
        try:
            with get_db() as (conn, cursor):
                cursor.execute(
                    "SELECT price_change, price_change_percent FROM etf_daily_data "
                    "WHERE ticker=%s ORDER BY date DESC LIMIT 1",
                    (ticker,)
                )
                prev_row = cursor.fetchone()
            if prev_row and prev_row.get("price_change") is not None:
                price_change     = float(prev_row["price_change"] or 0)
                price_change_pct = float(prev_row["price_change_percent"] or 0)
        except Exception as e:
            logger.debug(f"TW after-hours change DB fallback {ticker}: {e}")

    return {
        'ticker': ticker, 'current_price': price,
        'price_change': price_change,
        'price_change_percent': price_change_pct,
        'day_high': quote["day_high"], 'day_low': quote["day_low"],
        'fifty_two_week_high': wk52_h, 'fifty_two_week_low': wk52_l,
        'volume': quote["volume"],
        'asset_size': detail.get("asset_size", 0),
        'nav': None,  # 台股 NAV 由資產管理公司每日公告，無即時來源；None 存入 DB 讓前端顯示「暫無資料」
        'pe_ratio': detail.get("pe_ratio", 0),
        'expense_ratio': detail.get("expense_ratio", 0),
        'dividend_yield': div_yield,
        'payout_freq': payout_freq or "不配息",
        'annual_return_1y': ann_1y,
        'annual_return_3y': ann_3y,
        'annual_return_5y': ann_5y,
    }


def _fetch_us_etf(ticker: str) -> Optional[dict]:
    quote = _fetch_us_quote(ticker)
    if not quote:
        try:
            df = yf.download(ticker, period="10d", interval="1d", progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 2:
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                price = float(df['Close'].iloc[-1])
                prev  = float(df['Close'].iloc[-2])
                chg   = round(price - prev, 4)
                quote = {"current_price": price, "price_change": chg,
                         "price_change_percent": round(chg/prev*100, 4) if prev > 0 else 0,
                         "day_high": float(df['High'].iloc[-1]),
                         "day_low":  float(df['Low'].iloc[-1]),
                         "volume":   int(df['Volume'].iloc[-1])}
        except Exception as e:
            logger.debug(f"US yf fallback {ticker}: {e}")
    if not quote:
        return None

    price = quote["current_price"]
    history, div_yield, payout_freq = [], 0.0, "不配息"
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1mo&events=dividends"
        r = _get_with_retry(_new_session(f"https://finance.yahoo.com/quote/{ticker}"), url, timeout=12)
        if r and r.status_code == 200:
            res = r.json().get("chart", {}).get("result", [{}])[0]
            history = [safe_float(c) for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
            events  = res.get("events", {}).get("dividends", {})
            if events:
                cutoff = time.time() - 365 * 86400
                recent = [v["amount"] for v in events.values() if v.get("date", 0) >= cutoff]
                if recent:
                    div_yield   = round(sum(recent) / price * 100, 4)
                    payout_freq = "月配" if len(recent) >= 10 else "季配" if len(recent) >= 3 else "半年配" if len(recent) == 2 else "年配"
    except Exception as e:
        logger.debug(f"US history/dividend {ticker}: {e}")

    # 計算年化報酬需要「起點 + N 個月」共 N+1 筆，故 cutoff 用 -(N+1)
    cutoff_1y = len(history) - 13 if len(history) >= 13 else 0
    cutoff_3y = len(history) - 37 if len(history) >= 37 else 0
    ann_1y = _annualized_return(history[cutoff_1y:], 1.0)
    ann_3y = _annualized_return(history[cutoff_3y:], 3.0)
    ann_5y = _annualized_return(history, 5.0)
    last12 = history[-12:] if len(history) >= 12 else history
    wk52_h = max(last12) if last12 else quote["day_high"]
    wk52_l = min(last12) if last12 else quote["day_low"]

    detail = _fetch_yahoo_quotesummary(ticker)
    asset_size    = detail.get("asset_size", 0.0)
    pe_ratio      = detail.get("pe_ratio", 0.0)
    expense_ratio = detail.get("expense_ratio", 0.0)
    nav           = detail.get("nav") or price
    yf_yield      = detail.get("div_yield", 0.0)
    if yf_yield > div_yield:
        div_yield = round(yf_yield, 4)
        if div_yield > 0 and payout_freq == "不配息":
            payout_freq = "季配"

    return {
        'ticker': ticker, 'current_price': price,
        'price_change': quote["price_change"],
        'price_change_percent': quote["price_change_percent"],
        'day_high': quote["day_high"], 'day_low': quote["day_low"],
        'fifty_two_week_high': wk52_h, 'fifty_two_week_low': wk52_l,
        'volume': quote["volume"], 'asset_size': asset_size, 'nav': nav,
        'pe_ratio': pe_ratio, 'expense_ratio': expense_ratio,
        'dividend_yield': div_yield, 'payout_freq': payout_freq,
        'annual_return_1y': float(ann_1y) if ann_1y == ann_1y else 0.0,
        'annual_return_3y': float(ann_3y) if ann_3y == ann_3y else 0.0,
        'annual_return_5y': float(ann_5y) if ann_5y == ann_5y else 0.0,
    }


# ══════════════════════════════════════════════════════════
#  存入 DB
# ══════════════════════════════════════════════════════════

def save_etf_data(data: dict):
    today  = datetime.now().date()
    ticker  = data.get("ticker", "")
    cp      = safe_float(data.get("current_price"))
    nav_raw = data.get("nav")
    if nav_raw is None:
        nav = None
        dp  = 0.0
    else:
        nav = safe_float(nav_raw) or cp
        dp  = round((cp - nav) / nav * 100, 2) if nav > 0 and cp != nav else 0.0

    with get_db() as (conn, cursor):
        cursor.execute("""
            INSERT INTO etf_daily_data
            (ticker, date, current_price, price_change, price_change_percent,
             volume, asset_size, nav, discount_premium,
             dividend_yield, payout_freq,
             annual_return_1y, annual_return_3y, annual_return_5y,
             pe_ratio, expense_ratio,
             day_high, day_low, fifty_two_week_high, fifty_two_week_low)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              current_price=VALUES(current_price), price_change=VALUES(price_change),
              price_change_percent=VALUES(price_change_percent),
              volume=VALUES(volume), discount_premium=VALUES(discount_premium),
              dividend_yield=VALUES(dividend_yield), payout_freq=VALUES(payout_freq),
              annual_return_1y=VALUES(annual_return_1y),
              annual_return_3y=VALUES(annual_return_3y),
              annual_return_5y=VALUES(annual_return_5y),
              pe_ratio=IF(VALUES(pe_ratio)>0, VALUES(pe_ratio), pe_ratio),
              expense_ratio=IF(VALUES(expense_ratio)>0, VALUES(expense_ratio), expense_ratio),
              asset_size=IF(VALUES(asset_size)>0, VALUES(asset_size), asset_size),
              nav=IF(VALUES(nav)>0, VALUES(nav), nav),
              day_high=VALUES(day_high), day_low=VALUES(day_low),
              fifty_two_week_high=VALUES(fifty_two_week_high),
              fifty_two_week_low=VALUES(fifty_two_week_low)
        """, (
            ticker, today, cp,
            safe_float(data.get("price_change")),
            safe_float(data.get("price_change_percent")),
            safe_float(data.get("volume")), safe_float(data.get("asset_size")),
            nav, dp,
            data.get("dividend_yield"), data.get("payout_freq", "不配息"),
            safe_float(data.get("annual_return_1y")),
            safe_float(data.get("annual_return_3y")),
            safe_float(data.get("annual_return_5y")),
            safe_float(data.get("pe_ratio")),
            safe_float(data.get("expense_ratio")),
            safe_float(data.get("day_high")),
            safe_float(data.get("day_low")),
            safe_float(data.get("fifty_two_week_high")),
            safe_float(data.get("fifty_two_week_low")),
        ))
        conn.commit()
    cache.delete(f"detail:{ticker}")
    cache.delete_prefix("rank:")


# ── 需要 pandas ──
try:
    import pandas as pd
except ImportError:
    pd = None
