"""
etf_data.py — ETF 靜態清單、資料抓取、DB 存取
（從原 main.py 提取並整合 exchange_rate）
"""
from __future__ import annotations
import os, random, time, logging, threading
from datetime import datetime, date
from typing import Optional
from urllib.parse import quote as _url_quote

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


# ── Cloudflare Worker Proxy（繞過 Railway IP 被 Yahoo Finance 封鎖）──
# 環境變數設定說明：
#   CF_PROXY_URL    = https://<your-worker>.workers.dev
#   CF_PROXY_SECRET = <同 Worker 中設定的 SECRET>
CF_PROXY_URL    = os.environ.get("CF_PROXY_URL", "").rstrip("/")
CF_PROXY_SECRET = os.environ.get("CF_PROXY_SECRET", "")


def _cf_yahoo_get(url: str, timeout: int = 15) -> Optional[req_lib.Response]:
    """透過 Cloudflare Worker 代理呼叫 Yahoo Finance API。
    CF_PROXY_URL / CF_PROXY_SECRET 未設定時直接回傳 None（呼叫方自行 fallback）。
    Cloudflare IP 不在 Yahoo 封鎖名單內，可取得 TW ETF 被 Railway IP 封鎖時無法直連的資料。
    """
    if not CF_PROXY_URL or not CF_PROXY_SECRET:
        return None
    try:
        endpoint = f"{CF_PROXY_URL}?u={_url_quote(url, safe='')}"
        r = req_lib.get(endpoint, timeout=timeout, headers={"X-Proxy-Secret": CF_PROXY_SECRET})
        if r.status_code == 200:
            return r
        logger.debug(f"CF proxy HTTP {r.status_code} for {url[:70]}")
    except Exception as e:
        logger.debug(f"CF proxy error for {url[:70]}: {e}")
    return None


def _jitter(base: float = 1.0, spread: float = 2.0):
    """在下一次請求前加入隨機延遲，模擬人工瀏覽行為，避免固定節奏被偵測。"""
    time.sleep(base + random.uniform(0, spread))


def _get_with_retry(session: req_lib.Session, url: str, timeout: int = 6,
                    max_attempts: int = 3) -> Optional[req_lib.Response]:
    """帶指數退避的 GET，自動處理 429 限速與瞬斷重試。"""
    for attempt in range(max_attempts):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code == 429:
                wait = min(60, 30 * (2 ** attempt)) + random.uniform(0, 5)
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
            time.sleep(2 * (attempt + 1) + random.uniform(0, 1))
    return None


# ── Yahoo Finance crumb 快取（v10 API 認證用） ──
_yf_crumb: str = ""
_yf_crumb_cookies: dict = {}
_crumb_lock = threading.Lock()

def _refresh_yahoo_crumb() -> bool:
    """取得 Yahoo Finance crumb 認證。
    網路呼叫在鎖外執行，鎖只用於最後寫入共享狀態（避免鎖定期間所有讀取者阻塞）。
    """
    global _yf_crumb, _yf_crumb_cookies
    for attempt in range(3):
        try:
            s = _new_session()
            s.headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            r1 = s.get("https://fc.yahoo.com", timeout=8)
            if r1.status_code not in (200, 302, 303):
                time.sleep(5)
                continue
            r2 = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=8)
            if r2.status_code == 200 and r2.text and r2.text.strip() not in ("", "null"):
                crumb = r2.text.strip()
                cookies = dict(s.cookies)
                # 只在寫入共享狀態時短暫持鎖（毫秒級），不在網路 I/O 期間持鎖
                with _crumb_lock:
                    _yf_crumb = crumb
                    _yf_crumb_cookies = cookies
                logger.debug("Yahoo crumb 取得成功")
                return True
        except Exception as e:
            logger.debug(f"crumb refresh attempt {attempt+1}: {e}")
        time.sleep(10 * (attempt + 1))
    return False


def _yahoo_ticker(ticker: str, market: str) -> str:
    """回傳 Yahoo Finance 格式的代碼。
    台股債券 ETF（如 00679B）雖代碼以 B 結尾，但均在 TWSE 上市，用 .TW 而非 .TWO。
    .TWO 是 TPEX（上櫃）後綴，台股 ETF 幾乎全在 TWSE，一律用 .TW。
    """
    if market == "TW":
        return f"{ticker}.TW"
    return ticker


# ══════════════════════════════════════════════════════════
#  配息頻率靜態備援（當即時爬取失敗時確保標籤正確）
# ══════════════════════════════════════════════════════════
KNOWN_PAYOUT_FREQ: dict = {
    # ── 台股 ──
    '0050':   '半年配',  # 元大台灣50（2024起改半年配）
    '006208': '半年配',  # 富邦台50（每半年1次）
    '0056':   '季配',    # 元大高股息（2020Q4起改季配）
    '00878':  '季配',    # 國泰永續高股息
    '00919':  '月配',    # 群益台灣精選高息
    '00929':  '月配',    # 復華台灣科技優息
    '00713':  '季配',    # 元大台灣高息低波
    '00940':  '月配',    # 元大台灣價值高息
    '00939':  '月配',    # 統一台灣高息動能
    '00918':  '雙月配',  # 大華優利高填息30（每2個月1次）
    '00915':  '雙月配',  # 凱基優選高股息30（每2個月1次）
    '00900':  '季配',    # 富邦特選高股息30
    '00934':  '月配',    # 中信成長高股息
    '00701':  '季配',    # 國泰股利精選30
    '00891':  '季配',    # 中信關鍵半導體
    '00692':  '年配',    # 富邦公司治理
    '00646':  '年配',    # 元大S&P500
    '00662':  '年配',    # 富邦NASDAQ
    '00850':  '季配',    # 元大臺灣ESG永續
    '00757':  '季配',    # 統一FANG+
    '00762':  '季配',    # 元大全球AI
    '00830':  '季配',    # 國泰費城半導體
    '00881':  '季配',    # 國泰台灣5G+
    '00922':  '季配',    # 國泰台灣領袖50
    # ── 台股槓桿/反向/商品（不配息）──
    '00631L': '不配息',  # 元大台灣50正2
    '00632R': '不配息',  # 元大台灣50反1
    '00675L': '不配息',  # 富邦臺灣加權正2
    '00637L': '不配息',  # 元大滬深300正2
    '00715L': '不配息',  # 期街口布蘭特正2
    '00642U': '不配息',  # 期元大S&P石油
    # ── 台股其他 ──
    '0051':   '年配',    # 元大中型100
    '0052':   '年配',    # 富邦科技
    '0053':   '年配',    # 元大電子
    '00936':  '月配',    # 台新台灣永續高息
    '00944':  '月配',    # 群益半導體收益
    '00907':  '季配',    # 永豐優息存股
    '00892':  '季配',    # 富邦台灣半導體
    '00861':  '季配',    # 元大臺灣ESG永續ETF
    '00679B': '半年配',  # 元大美債20年
    '00687B': '半年配',  # 國泰20年美債
    '00695B': '半年配',  # 富邦美債20年
    '00720B': '季配',    # 元大投資級公司債
    '006205': '年配',    # 富邦上証
    # ── 美股 ──
    'SPY':    '季配',
    'VOO':    '季配',
    'IVV':    '季配',
    'VTI':    '季配',
    'QQQ':    '季配',
    'SCHD':   '季配',
    'VUG':    '季配',    # Vanguard Growth ETF（每季配息：3/6/9/12月）
    'VT':     '季配',    # Vanguard Total World（每季配息）
    'IWM':    '季配',
    'DIA':    '月配',
    'JEPI':   '月配',
    'JEPQ':   '月配',    # JPMorgan Nasdaq Premium Income（月配）
    'VYM':    '季配',
    'XLK':    '季配',
    'SOXX':   '季配',
    'SMH':    '季配',
    'XLF':    '季配',
    'XLE':    '季配',
    'VNQ':    '季配',
    'ARKK':   '不配息',
    'TQQQ':   '不配息',  # 槓桿型
    'SQQQ':   '不配息',  # 反向型
    'UPRO':   '不配息',  # 槓桿型
    'GLD':    '不配息',
    'SLV':    '不配息',
    'IBIT':   '不配息',  # Bitcoin ETF
    'FBTC':   '不配息',  # Bitcoin ETF
    'TLT':    '月配',
    'AGG':    '月配',
    'BND':    '月配',
    # ── 美股其他 ──
    'SOXL':   '季配',
    'VIG':    '季配',
    'VGT':    '季配',
    'IEF':    '月配',
    'VEA':    '季配',
    'VWO':    '季配',
    'EEM':    '半年配',
    'XLV':    '季配',
}


# ══════════════════════════════════════════════════════════
#  年配息金額靜態備援（Yahoo 在 Railway 環境常被封鎖，此為最終 fallback）
#  單位：TWD / 股 / 年（四捨五入至小數一位）
#  資料來源：近 4 季或 2 期配息合計（更新基準：2025-Q1）
#  注意：此為近似值，實際殖利率以即時爬取為準；每年建議人工校對一次。
# ══════════════════════════════════════════════════════════
KNOWN_ANNUAL_DIVIDEND: dict = {
    # ── 台股寬基 ──
    '0050':   3.40,  '006208': 4.50,  '0051':   1.20,
    '0052':   3.00,  '0053':   1.50,
    '00850':  1.60,  '00692':  4.50,
    '00646':  0.00,  '00662':  0.00,  # 海外追蹤型，配息極少
    # ── 高股息 ──
    '0056':   3.50,  '00878':  1.75,  '00713':  2.68,
    '00919':  2.52,  '00929':  1.80,  '00940':  0.90,
    '00939':  1.44,  '00934':  1.68,  '00918':  1.80,
    '00915':  1.60,  '00900':  2.40,  '00701':  2.20,
    '00891':  1.00,  '00936':  1.56,  '00944':  2.00,  # 00891 半導體主題，非高股息，年息約1元
    '00907':  1.80,  '00892':  1.20,
    # ── 科技/主題 ──
    '00881':  1.00,  '00757':  2.50,  '00762':  0.50,
    '00830':  0.80,  '00922':  0.60,
    # ── 槓桿/反向/商品（不配息）──
    '00631L': 0.00,  '00632R': 0.00,  '00675L': 0.00,
    '00637L': 0.00,  '00715L': 0.00,  '00642U': 0.00,
    # ── 其他台股 ──
    '00861':  1.00,  '006205': 0.50,  # 00861 元大臺灣ESG永續ETF（季配，年息約1元）
    # ── 債券 ETF ──
    '00679B': 0.42,  '00687B': 0.38,
    '00695B': 0.36,  '00720B': 0.48,
}


# ══════════════════════════════════════════════════════════
#  美股靜態殖利率備援（%，與股價無關，股票分割不影響）
#  用途：Railway IP 被 Yahoo Finance 封鎖時，確保高股息 ETF 不顯示「—」
#  更新原則：殖利率顯著偏離（>0.5pp）時更新；不需要跟著每次配息改
# ══════════════════════════════════════════════════════════
KNOWN_YIELD_US: dict = {
    # ── 大盤寬基 ──
    'SPY':  1.25,  'VOO':  1.30,  'IVV':  1.25,  'VTI':  1.30,
    'QQQ':  0.50,  'VT':   2.00,  'IWM':  1.40,  'DIA':  1.80,
    # ── 高股息（最重要：殖利率高，顯示「—」最損公信力）──
    'SCHD': 3.50,  'VYM':  3.00,  'JEPI': 7.50,  'JEPQ': 9.50,
    # ── 科技/半導體 ──
    'XLK':  0.42,  'SOXX': 0.80,  'SMH':  0.60,  'ARKK': 0.00,
    # ── 槓桿 ──
    'SOXL': 1.00,  'TQQQ': 0.00,  'SQQQ': 0.00,  'UPRO': 0.00,
    # ── 類股 ──
    'XLF':  2.00,  'XLE':  3.00,  'VNQ':  3.50,  'XLV':  1.50,
    # ── 國際市場 ──
    'VEA':  3.00,  'VWO':  3.00,  'EEM':  2.50,
    # ── 成長/因子 ──
    'VIG':  1.80,  'VGT':  0.50,
    # ── 原物料/債券 ──
    'GLD':  0.00,  'SLV':  0.00,
    'TLT':  4.50,  'AGG':  4.00,  'BND':  4.00,  'IEF':  4.00,
    # ── 加密 ──
    'IBIT': 0.00,  'FBTC': 0.00,
}


# ══════════════════════════════════════════════════════════
#  費用率靜態備援（Yahoo v10 對台股常無法取得費用率）
# ══════════════════════════════════════════════════════════
KNOWN_EXPENSE_RATIO: dict = {
    # ── 台股（單位：小數，例 0.0046 = 0.46%）──
    # 寬基
    '0050':   0.0046, '006208': 0.0046, '0056':   0.0066,
    '00850':  0.0060, '00692':  0.0061,
    # 海外指數
    '00646':  0.0099, '00662':  0.0099,
    # 高股息
    '00878':  0.0065, '00919':  0.0090, '00929':  0.0095,
    '00713':  0.0045, '00940':  0.0065, '00939':  0.0080,
    '00918':  0.0095, '00915':  0.0080, '00900':  0.0090,
    '00934':  0.0085, '00701':  0.0065,
    # 科技/主題
    '00881':  0.0065, '00757':  0.0099, '00762':  0.0099,
    '00830':  0.0065, '00891':  0.0075, '00922':  0.0090,
    # 槓桿/反向/商品（費用率通常較高）
    '00631L': 0.0100, '00632R': 0.0100, '00675L': 0.0100,
    '00637L': 0.0100, '00715L': 0.0150, '00642U': 0.0150,
    # 其他台股
    '0051':   0.0044, '0052':   0.0053, '0053':   0.0044,
    '00936':  0.0085, '00944':  0.0080, '00907':  0.0075,
    '00892':  0.0065, '00861':  0.0080,
    '00679B': 0.0015, '00687B': 0.0017, '00695B': 0.0020,
    '00720B': 0.0025, '006205': 0.0099,
    # ── 美股（通常 Yahoo 可取得，此處備援）──
    # 大盤
    'SPY':  0.0009, 'VOO':  0.0003, 'IVV':  0.0003, 'VTI':  0.0003,
    'QQQ':  0.0020, 'VT':   0.0007, 'VUG':  0.0004, 'IWM':  0.0019,
    'DIA':  0.0016,
    # 股息
    'SCHD': 0.0006, 'VYM':  0.0006, 'JEPI': 0.0035, 'JEPQ': 0.0035,
    # 科技/半導體
    'XLK':  0.0009, 'SOXX': 0.0035, 'SMH':  0.0035, 'ARKK': 0.0075,
    # 槓桿/反向
    'TQQQ': 0.0088, 'SQQQ': 0.0095, 'UPRO': 0.0093,
    # 類股
    'XLF':  0.0009, 'XLE':  0.0009, 'VNQ':  0.0013,
    # 原物料/加密
    'GLD':  0.0040, 'SLV':  0.0050, 'IBIT': 0.0025, 'FBTC': 0.0025,
    # 債券
    'TLT':  0.0015, 'AGG':  0.0003, 'BND':  0.0003,
    # 其他美股
    'SOXL': 0.0176, 'VIG':  0.0006, 'VGT':  0.0010, 'IEF':  0.0015,
    'VEA':  0.0005, 'VWO':  0.0008, 'EEM':  0.0068, 'XLV':  0.0009,
}


def _classify_freq(n: int) -> str:
    """根據過去 12 個月內的「個別配息事件次數」判斷配息頻率。

    月配   (12x/年): n ≥  9  ── 允許最多 3 次未入帳/缺失
    雙月配  ( 6x/年): n ≥  5  ── 每 2 個月配 1 次（6次/年，允許1次缺失）
    季配   ( 4x/年): n ≥  3  ── 每季配 1 次（允許1次缺失）
    半年配  ( 2x/年): n == 2
    年配   ( 1x/年): n == 1
    不配息:          n == 0
    """
    if n >= 9: return "月配"
    if n >= 5: return "雙月配"
    if n >= 3: return "季配"
    if n == 2: return "半年配"
    if n == 1: return "年配"
    return "不配息"


# 頻率等級對照（數字越大頻率越高）
_FREQ_RANK: dict = {'不配息': 0, '年配': 1, '半年配': 2, '季配': 3, '雙月配': 4, '月配': 5}


def _best_freq(yf_count: int, ticker: str) -> str:
    """取 Yahoo 事件數推算頻率 與 靜態備援 兩者中等級較高的。

    設計原則：
    - Yahoo 事件數 > 靜態備援：ETF 升頻（例如年配→半年配），信任最新資料。
    - 靜態備援 > Yahoo 事件數：Yahoo 漏抓事件（常見於台股月配），以靜態備援校正。
    - 若靜態備援無記錄：直接使用 Yahoo 推算結果。
    """
    yf_freq    = _classify_freq(yf_count)
    known_freq = KNOWN_PAYOUT_FREQ.get(ticker, '')
    if not known_freq:
        return yf_freq
    return (known_freq if _FREQ_RANK.get(known_freq, 0) >= _FREQ_RANK.get(yf_freq, 0)
            else yf_freq)


def _save_dividend_events(ticker: str, events: list[tuple]):
    """將配息事件批次寫入 etf_dividends 表，供回測 DRIP 使用。

    events: [(ticker, ex_date_str, amount_float), ...]
    ON DUPLICATE KEY UPDATE amount=VALUES(amount)：允許金額更正（如除息日補正）。
    """
    if not events:
        return
    try:
        with get_db() as (conn, cursor):
            for _ticker, ex_date, amount in events:
                if amount and float(amount) > 0:
                    cursor.execute(
                        "INSERT INTO etf_dividends (ticker, ex_date, amount) "
                        "VALUES (%s, %s, %s) "
                        "ON DUPLICATE KEY UPDATE amount=VALUES(amount)",
                        (_ticker, ex_date, float(amount)),
                    )
            conn.commit()
        logger.debug(f"dividend events saved: {ticker} ({len(events)} events)")
    except Exception as e:
        logger.debug(f"save_dividend_events {ticker}: {e}")


def _annualized_return(closes: list, years: float) -> Optional[float]:
    """回傳年化報酬率（%）。資料不足（< 5 筆）時回傳 None，讓呼叫端可區分「計算結果為 0」與「資料不足」。"""
    if not closes or len(closes) < 5:
        return None   # 明確回傳 None，由 save_etf_data 存入 DB NULL，前端顯示「—」
    try:
        p0, p1 = float(closes[0]), float(closes[-1])
        if p0 <= 0:
            return None
        total = (p1 - p0) / p0
        if years < 1:
            return round(total * 100, 2)
        return round(((1 + total) ** (1 / years) - 1) * 100, 2)
    except Exception as e:
        logger.debug(f"annualized_return calc: {e}")
        return None


# ══════════════════════════════════════════════════════════
#  ETF 靜態清單
# ══════════════════════════════════════════════════════════

TW_ETFS = [
    # ── 寬基指數 ──
    {'ticker': '0050',   'name': '元大台灣50',              'market': 'TW', 'hot': True, 'issuer': '元大投信', 'listing_date': '2003-06-25', 'category': 'broad_market'},
    {'ticker': '006208', 'name': '富邦台50',                'market': 'TW', 'hot': True, 'issuer': '富邦投信', 'listing_date': '2012-07-17', 'category': 'broad_market'},
    {'ticker': '00646',  'name': '元大S&P500',              'market': 'TW', 'hot': True, 'issuer': '元大投信', 'listing_date': '2015-12-17', 'category': 'broad_market'},
    # ── ESG ──
    {'ticker': '00850',  'name': '元大臺灣ESG永續',         'market': 'TW', 'hot': True, 'issuer': '元大投信', 'listing_date': '2019-08-23', 'category': 'esg'},
    # ── 高股息 ──
    {'ticker': '0056',   'name': '元大高股息',              'market': 'TW', 'hot': True, 'issuer': '元大投信', 'listing_date': '2007-12-26', 'category': 'high_dividend'},
    {'ticker': '00878',  'name': '國泰永續高股息',          'market': 'TW', 'hot': True, 'issuer': '國泰投信', 'listing_date': '2020-07-20', 'category': 'high_dividend'},
    {'ticker': '00919',  'name': '群益台灣精選高息',        'market': 'TW', 'hot': True, 'issuer': '群益投信', 'listing_date': '2022-10-20', 'category': 'high_dividend'},
    {'ticker': '00929',  'name': '復華台灣科技優息',        'market': 'TW', 'hot': True, 'issuer': '復華投信', 'listing_date': '2023-03-31', 'category': 'high_dividend'},
    {'ticker': '00713',  'name': '元大台灣高息低波',        'market': 'TW', 'hot': True, 'issuer': '元大投信', 'listing_date': '2017-09-27', 'category': 'high_dividend'},
    {'ticker': '00940',  'name': '元大台灣價值高息',        'market': 'TW', 'hot': True, 'issuer': '元大投信', 'listing_date': '2024-03-20', 'category': 'high_dividend'},
    {'ticker': '00939',  'name': '統一台灣高息動能',        'market': 'TW', 'hot': True, 'issuer': '統一投信', 'listing_date': '2023-07-04', 'category': 'high_dividend'},
    {'ticker': '00915',  'name': '凱基優選高股息30',        'market': 'TW', 'hot': True, 'issuer': '凱基投信', 'listing_date': '2022-04-12', 'category': 'high_dividend'},
    {'ticker': '00900',  'name': '富邦特選高股息30',        'market': 'TW', 'hot': True, 'issuer': '富邦投信', 'listing_date': '2021-12-15', 'category': 'high_dividend'},
    {'ticker': '00934',  'name': '中信成長高股息',          'market': 'TW', 'hot': True, 'issuer': '中國信託投信', 'listing_date': '2023-06-07', 'category': 'high_dividend'},
    # ── 產業 / 主題 ──
    {'ticker': '00757',  'name': '統一FANG+',               'market': 'TW', 'hot': True, 'issuer': '統一投信',     'listing_date': '2018-12-13', 'category': 'sector'},
    {'ticker': '00891',  'name': '中信關鍵半導體',          'market': 'TW', 'hot': True, 'issuer': '中國信託投信', 'listing_date': '2021-10-19', 'category': 'sector'},
    {'ticker': '00830',  'name': '國泰費城半導體',          'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2020-01-09', 'category': 'sector'},
    {'ticker': '00882',  'name': '中信美國科技',            'market': 'TW', 'hot': True, 'issuer': '中國信託投信', 'listing_date': '2021-01-19', 'category': 'sector'},
    {'ticker': '00881',  'name': '國泰台灣5G+',             'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2020-10-15', 'category': 'sector'},
    {'ticker': '00921',  'name': '兆豐台灣晶圓製造',        'market': 'TW', 'hot': True, 'issuer': '兆豐投信',     'listing_date': '2022-05-17', 'category': 'sector'},
    {'ticker': '00896',  'name': '中信綠能及電動車',        'market': 'TW', 'hot': True, 'issuer': '中國信託投信', 'listing_date': '2021-12-15', 'category': 'sector'},
    {'ticker': '00912',  'name': '中信臺灣智慧50',          'market': 'TW', 'hot': True, 'issuer': '中國信託投信', 'listing_date': '2022-07-26', 'category': 'sector'},
    # ── 高股息（補充）──
    {'ticker': '00927',  'name': '群益半導體收益',          'market': 'TW', 'hot': True, 'issuer': '群益投信',     'listing_date': '2022-12-13', 'category': 'high_dividend'},
    {'ticker': '00918',  'name': '大華優利高填息30',        'market': 'TW', 'hot': True, 'issuer': '大華投信',     'listing_date': '2022-08-09', 'category': 'high_dividend'},
    {'ticker': '00922',  'name': '國泰台灣領袖50',          'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2023-03-14', 'category': 'high_dividend'},
    {'ticker': '00932',  'name': '兆豐永續高息等權重',      'market': 'TW', 'hot': True, 'issuer': '兆豐投信',     'listing_date': '2023-05-02', 'category': 'high_dividend'},
    {'ticker': '00907',  'name': '永豐優息存股',            'market': 'TW', 'hot': True, 'issuer': '永豐投信',     'listing_date': '2022-09-08', 'category': 'high_dividend'},
    # ── 寬基補充 ──
    {'ticker': '00733',  'name': '富邦台灣中小',            'market': 'TW', 'hot': True, 'issuer': '富邦投信',     'listing_date': '2014-05-30', 'category': 'broad_market'},
    # ── 產業補充 ──
    {'ticker': '00762',  'name': '富邦NASDAQ',              'market': 'TW', 'hot': True, 'issuer': '富邦投信',     'listing_date': '2018-04-17', 'category': 'sector'},
    {'ticker': '00861',  'name': '元大全球AI',              'market': 'TW', 'hot': True, 'issuer': '元大投信',     'listing_date': '2021-07-01', 'category': 'sector'},
    {'ticker': '00875',  'name': '國泰智能電動車',          'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2021-07-13', 'category': 'sector'},
    {'ticker': '00887',  'name': '永豐美國科技',            'market': 'TW', 'hot': True, 'issuer': '永豐投信',     'listing_date': '2021-09-16', 'category': 'sector'},
    {'ticker': '00874',  'name': '國泰網路資安',            'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2021-03-23', 'category': 'sector'},
    # ── ESG 補充 ──
    {'ticker': '00923',  'name': '群益台ESG低碳50',         'market': 'TW', 'hot': True, 'issuer': '群益投信',     'listing_date': '2023-05-09', 'category': 'esg'},
    # ── 債券 ──
    {'ticker': '00679B', 'name': '元大美債20年',            'market': 'TW', 'hot': True, 'issuer': '元大投信',     'listing_date': '2017-01-11', 'category': 'bond'},
    {'ticker': '00687B', 'name': '國泰20年美債',            'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2017-05-02', 'category': 'bond'},
    {'ticker': '00696B', 'name': '富邦美債7至10年',         'market': 'TW', 'hot': True, 'issuer': '富邦投信',     'listing_date': '2017-09-29', 'category': 'bond'},
    {'ticker': '00720B', 'name': '元大投資級公司債',        'market': 'TW', 'hot': True, 'issuer': '元大投信',     'listing_date': '2018-07-10', 'category': 'bond'},
    {'ticker': '00725B', 'name': '國泰投資級公司債',        'market': 'TW', 'hot': True, 'issuer': '國泰投信',     'listing_date': '2018-11-15', 'category': 'bond'},
    {'ticker': '00695B', 'name': '富邦美債1至3年',          'market': 'TW', 'hot': True, 'issuer': '富邦投信',     'listing_date': '2017-09-29', 'category': 'bond'},
]

US_ETFS = [
    # ── 大盤指數 ──
    {'ticker': 'SPY',  'name': 'SPDR S&P 500 ETF Trust',               'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1993-01-22', 'category': 'broad_market'},
    {'ticker': 'VOO',  'name': 'Vanguard S&P 500 ETF',                 'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2010-09-07', 'category': 'broad_market'},
    {'ticker': 'IVV',  'name': 'iShares Core S&P 500 ETF',             'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2000-05-15', 'category': 'broad_market'},
    {'ticker': 'VTI',  'name': 'Vanguard Total Stock Market ETF',      'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2001-05-24', 'category': 'broad_market'},
    {'ticker': 'IWM',  'name': 'iShares Russell 2000 ETF',             'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2000-05-22', 'category': 'broad_market'},
    {'ticker': 'DIA',  'name': 'SPDR Dow Jones Industrial Average ETF','market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-01-14', 'category': 'broad_market'},
    # ── 國際 ──
    {'ticker': 'VT',   'name': 'Vanguard Total World Stock ETF',       'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2008-06-24', 'category': 'international'},
    {'ticker': 'VXUS', 'name': 'Vanguard Total International Stock ETF','market': 'US', 'hot': True, 'issuer': 'Vanguard',    'listing_date': '2011-01-26', 'category': 'international'},
    # ── 股息 ──
    {'ticker': 'SCHD', 'name': 'Schwab U.S. Dividend Equity ETF',      'market': 'US', 'hot': True, 'issuer': 'Schwab',       'listing_date': '2011-10-20', 'category': 'high_dividend'},
    {'ticker': 'VYM',  'name': 'Vanguard High Dividend Yield ETF',     'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2006-11-10', 'category': 'high_dividend'},
    {'ticker': 'JEPI', 'name': 'JPMorgan Equity Premium Income ETF',   'market': 'US', 'hot': True, 'issuer': 'JPMorgan',     'listing_date': '2020-05-20', 'category': 'high_dividend'},
    {'ticker': 'JEPQ', 'name': 'JPMorgan Nasdaq Equity Premium Income ETF','market': 'US', 'hot': True, 'issuer': 'JPMorgan', 'listing_date': '2022-05-03', 'category': 'high_dividend'},
    {'ticker': 'DGRO', 'name': 'iShares Core Dividend Growth ETF',     'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2014-06-10', 'category': 'high_dividend'},
    # ── 科技 / 半導體 ──
    {'ticker': 'QQQ',  'name': 'Invesco QQQ Trust',                    'market': 'US', 'hot': True, 'issuer': 'Invesco',      'listing_date': '1999-03-10', 'category': 'sector'},
    {'ticker': 'XLK',  'name': 'Technology Select Sector SPDR Fund',   'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'SOXX', 'name': 'iShares Semiconductor ETF',            'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2001-07-10', 'category': 'sector'},
    {'ticker': 'SMH',  'name': 'VanEck Semiconductor ETF',             'market': 'US', 'hot': True, 'issuer': 'VanEck',       'listing_date': '2011-12-20', 'category': 'sector'},
    # ── 類股 ──
    {'ticker': 'XLF',  'name': 'Financial Select Sector SPDR Fund',    'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'XLE',  'name': 'Energy Select Sector SPDR Fund',       'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'VNQ',  'name': 'Vanguard Real Estate ETF',             'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2004-09-23', 'category': 'sector'},
    # ── 債券 ──
    {'ticker': 'TLT',  'name': 'iShares 20+ Year Treasury Bond ETF',   'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2002-07-22', 'category': 'bond'},
    {'ticker': 'AGG',  'name': 'iShares Core U.S. Aggregate Bond ETF', 'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2003-09-22', 'category': 'bond'},
    {'ticker': 'BND',  'name': 'Vanguard Total Bond Market ETF',       'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2007-04-03', 'category': 'bond'},
    # ── 另類資產 ──
    {'ticker': 'GLD',  'name': 'SPDR Gold Shares',                     'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '2004-11-18', 'category': 'alternative'},
    {'ticker': 'IAU',  'name': 'iShares Gold Trust',                   'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2005-01-21', 'category': 'alternative'},
    {'ticker': 'SLV',  'name': 'iShares Silver Trust',                 'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2006-01-21', 'category': 'alternative'},
    {'ticker': 'IBIT', 'name': 'iShares Bitcoin Trust ETF',            'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2024-01-11', 'category': 'alternative'},
    # ── 國際市場 ──
    {'ticker': 'EFA',  'name': 'iShares MSCI EAFE ETF',                'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2001-08-14', 'category': 'international'},
    {'ticker': 'VEA',  'name': 'Vanguard FTSE Developed Markets ETF',  'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2007-07-20', 'category': 'international'},
    {'ticker': 'EEM',  'name': 'iShares MSCI Emerging Markets ETF',    'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2003-04-07', 'category': 'international'},
    {'ticker': 'VWO',  'name': 'Vanguard FTSE Emerging Markets ETF',   'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2005-03-04', 'category': 'international'},
    # ── 成長型 ──
    {'ticker': 'VUG',  'name': 'Vanguard Growth ETF',                  'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2004-01-26', 'category': 'growth'},
    {'ticker': 'SCHG', 'name': 'Schwab U.S. Large-Cap Growth ETF',     'market': 'US', 'hot': True, 'issuer': 'Schwab',       'listing_date': '2009-12-11', 'category': 'growth'},
    {'ticker': 'QQQM', 'name': 'Invesco NASDAQ 100 ETF',               'market': 'US', 'hot': True, 'issuer': 'Invesco',      'listing_date': '2020-10-13', 'category': 'growth'},
    # ── 價值型 ──
    {'ticker': 'VTV',  'name': 'Vanguard Value ETF',                   'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2004-01-26', 'category': 'broad_market'},
    {'ticker': 'SPLG', 'name': 'SPDR Portfolio S&P 500 ETF',           'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '2005-11-08', 'category': 'broad_market'},
    {'ticker': 'VB',   'name': 'Vanguard Small-Cap ETF',               'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2004-01-26', 'category': 'broad_market'},
    {'ticker': 'MDY',  'name': 'SPDR S&P MidCap 400 ETF',             'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1995-05-04', 'category': 'broad_market'},
    # ── 股息補充 ──
    {'ticker': 'VIG',  'name': 'Vanguard Dividend Appreciation ETF',   'market': 'US', 'hot': True, 'issuer': 'Vanguard',     'listing_date': '2006-04-21', 'category': 'high_dividend'},
    {'ticker': 'HDV',  'name': 'iShares Core High Dividend ETF',       'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2011-03-29', 'category': 'high_dividend'},
    # ── 類股補充 ──
    {'ticker': 'XLV',  'name': 'Health Care Select Sector SPDR Fund',  'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'XLY',  'name': 'Consumer Discretionary Select SPDR',   'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'XLI',  'name': 'Industrial Select Sector SPDR Fund',   'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'XLP',  'name': 'Consumer Staples Select Sector SPDR',  'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'XLU',  'name': 'Utilities Select Sector SPDR Fund',    'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '1998-12-16', 'category': 'sector'},
    {'ticker': 'XBI',  'name': 'SPDR S&P Biotech ETF',                 'market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '2006-01-31', 'category': 'sector'},
    # ── 債券補充 ──
    {'ticker': 'HYG',  'name': 'iShares iBoxx $ High Yield Corporate Bond ETF','market': 'US', 'hot': True, 'issuer': 'BlackRock', 'listing_date': '2007-04-04', 'category': 'bond'},
    {'ticker': 'LQD',  'name': 'iShares iBoxx $ Investment Grade Corporate Bond ETF','market': 'US', 'hot': True, 'issuer': 'BlackRock', 'listing_date': '2002-07-22', 'category': 'bond'},
    {'ticker': 'IEF',  'name': 'iShares 7-10 Year Treasury Bond ETF',  'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2002-07-22', 'category': 'bond'},
    {'ticker': 'SHY',  'name': 'iShares 1-3 Year Treasury Bond ETF',   'market': 'US', 'hot': True, 'issuer': 'BlackRock',    'listing_date': '2002-07-22', 'category': 'bond'},
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
    """將靜態清單的 ETF 代碼與名稱寫入 etf_master，不寫入任何假價格。

    策略：
      1. 38 檔熱門 ETF → INSERT/UPDATE，確保 is_hot=1
      2. 已在 DB 但不在熱門清單的舊 ETF → is_hot 重設為 0
         （避免歷史殘留 ETF 佔用排程更新資源）
    """
    hot_tickers = [e['ticker'] for e in ALL_ETFS]   # ALL_ETFS = 全部 38 檔，均 hot=True
    with get_db() as (conn, cursor):
        # Step 1: 插入 / 更新熱門 ETF，強制設 is_hot=1
        for etf in ALL_ETFS:
            issuer       = etf.get('issuer') or ''
            listing_date = etf.get('listing_date') or None
            category     = etf.get('category') or None
            cursor.execute(
                "INSERT INTO etf_master (ticker, name, market, is_hot, issuer, listing_date, category) "
                "VALUES (%s, %s, %s, 1, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE "
                "name=VALUES(name), market=VALUES(market), is_hot=1, "
                "issuer=IF(VALUES(issuer)!='', VALUES(issuer), issuer), "
                "listing_date=IF(VALUES(listing_date) IS NOT NULL, VALUES(listing_date), listing_date), "
                "category=IF(VALUES(category) IS NOT NULL, VALUES(category), category)",
                (etf['ticker'], etf['name'], etf['market'], issuer, listing_date, category)
            )

        # Step 2: 將不再熱門的舊 ETF 設 is_hot=0，停止自動排程更新
        if hot_tickers:
            fmt = ",".join(["%s"] * len(hot_tickers))
            cursor.execute(
                f"UPDATE etf_master SET is_hot=0 "
                f"WHERE is_hot=1 AND ticker NOT IN ({fmt})",
                hot_tickers,
            )
            demoted = cursor.rowcount
            if demoted:
                logger.info(f"🔄 {demoted} 檔 ETF 移出熱門清單（is_hot → 0）")

        conn.commit()
    logger.info(f"✅ etf_master 種子資料完成（{len(ALL_ETFS)} 檔熱門 ETF，含 category）")


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
    """取得台股 ETF 配息資料，回傳 (dividend_yield_pct, payout_freq, confirmed)。

    confirmed=True  → API 成功回應（即使 dividend=0 也可信，可覆蓋 DB 舊值）
    confirmed=False → 所有 API 失敗，回傳靜態備援值，不應覆蓋 DB 中的合理舊值

    來源優先順序：
    1. Yahoo Finance chart events — 個別配息事件，可準確計算頻率
    2. TWSE TWT48U — 每筆為「年度彙總」，只取最近年度金額估算殖利率；
       頻率無法從此端點判斷，改用靜態備援 KNOWN_PAYOUT_FREQ
    3. 靜態備援 (confirmed=False) — 確保 payout_freq 不因爬取失敗而標成「不配息」
    """
    primary = _yahoo_ticker(ticker, "TW")
    alt     = f"{ticker}.TWO" if primary.endswith(".TW") else f"{ticker}.TW"

    # 1. Yahoo Finance events（個別事件，頻率最準確）
    # CF Proxy 優先 → 直連 fallback（Railway 上 Yahoo 封鎖 TW ETF 直連）
    for yt in (primary, alt):
        try:
            url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}"
                   f"?range=5y&interval=1mo&events=dividends")
            r = (_cf_yahoo_get(url, timeout=15)
                 or _get_with_retry(_new_session(f"https://finance.yahoo.com/quote/{yt}"), url, timeout=10))
            if r and r.status_code == 200:
                result = r.json().get("chart", {}).get("result")
                if result:
                    events = result[0].get("events", {}).get("dividends", {})

                    # 持久化全部 5 年配息事件到 etf_dividends（回測 DRIP 使用）
                    if events:
                        all_ev = [
                            (ticker,
                             datetime.utcfromtimestamp(v["date"]).strftime("%Y-%m-%d"),
                             safe_float(v.get("amount", 0)))
                            for v in events.values()
                            if safe_float(v.get("amount", 0)) > 0
                        ]
                        _save_dividend_events(ticker, all_ev)

                    cutoff = time.time() - 365 * 86400
                    recent = [v["amount"] for v in events.values()
                              if v.get("date", 0) >= cutoff and safe_float(v.get("amount", 0)) > 0]
                    if recent and current_price > 0:
                        dy   = round(sum(recent) / current_price * 100, 4)
                        # 取 Yahoo 事件數 與 靜態備援 兩者中頻率等級較高者（防止漏抓或升頻）
                        freq = _best_freq(len(recent), ticker)
                        return dy, freq, True  # confirmed=True：Yahoo 明確確認有配息
                    # Yahoo 成功回應但 recent=0 → 已確認近 12 個月無配息事件
                    freq = KNOWN_PAYOUT_FREQ.get(ticker, "不配息")
                    return 0.0, freq, True  # confirmed=True：可信賴的 0（非 API 失敗）
        except Exception as e:
            logger.debug(f"TW dividend Yahoo {yt}: {e}")
        _jitter(0.5, 1.5)

    # 2. TWSE TWT48U（每筆 = 一個年度彙總，column[1] = 現金股利合計）
    #    只取最近一筆有效金額估算殖利率；頻率改由靜態備援決定
    if ticker[-1].isdigit():
        try:
            url = (f"https://www.twse.com.tw/exchangeReport/TWT48U"
                   f"?response=json&stockNo={ticker}")
            r = _get_with_retry(_new_session("https://www.twse.com.tw/"), url, timeout=10)
            rows = r.json().get("data", []) if r else []
            if rows:
                for row in reversed(rows):          # 最新年度在後，倒序找第一筆有效值
                    if not isinstance(row, list) or len(row) < 2:
                        continue
                    try:
                        div_amt = float(str(row[1]).replace(",", "").strip())
                        if 0.01 < div_amt < 200 and current_price > 0:
                            dy   = round(div_amt / current_price * 100, 4)
                            # TWSE TWT48U 無法判斷頻率，直接用靜態備援
                            freq = KNOWN_PAYOUT_FREQ.get(ticker) or "年配"
                            return dy, freq, True  # confirmed=True：TWSE 官方確認
                    except (ValueError, TypeError):
                        continue
        except Exception as e:
            logger.debug(f"TW dividend TWSE {ticker}: {e}")

    # 3. 靜態備援（confirmed=False）：所有 API 失敗，回傳估算值但不覆蓋 DB 舊值
    if ticker in KNOWN_PAYOUT_FREQ:
        freq = KNOWN_PAYOUT_FREQ[ticker]
        if ticker in KNOWN_ANNUAL_DIVIDEND and current_price > 0:
            approx_dy = round(KNOWN_ANNUAL_DIVIDEND[ticker] / current_price * 100, 4)
            return approx_dy, freq, False  # confirmed=False：靜態估算，不可覆蓋 DB

        return 0.0, freq, False

    if ticker in KNOWN_ANNUAL_DIVIDEND and current_price > 0:
        approx_dy = round(KNOWN_ANNUAL_DIVIDEND[ticker] / current_price * 100, 4)
        return approx_dy, "不配息", False

    return 0.0, "不配息", False


def _fetch_tw_history(ticker: str) -> list:
    """取得 5 年月線收盤價。
    優先走 Cloudflare Worker Proxy（繞過 Railway IP 封鎖），
    CF 未設定或失敗時直連 Yahoo Finance，自動嘗試 .TW / .TWO 兩種後綴。
    """
    primary = _yahoo_ticker(ticker, "TW")
    alt     = f"{ticker}.TWO" if primary.endswith(".TW") else f"{ticker}.TW"
    for yt in (primary, alt):
        try:
            url = f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}?range=5y&interval=1mo"
            # CF Proxy 優先 → 直連 fallback
            r = (_cf_yahoo_get(url, timeout=15)
                 or _get_with_retry(_new_session(f"https://finance.yahoo.com/quote/{yt}"), url, timeout=6))
            if r and r.status_code == 200:
                result = r.json().get("chart", {}).get("result")
                if result:
                    closes = [safe_float(c) for c in
                              (result[0].get("indicators", {}).get("quote", [{}])[0].get("close") or [])
                              if c is not None]
                    if len(closes) >= 6:
                        return closes
        except Exception as e:
            logger.debug(f"TW history Yahoo {yt}: {e}")
    return []


def _fetch_52week_hl_db(ticker: str, fallback_price: float) -> tuple[float, float]:
    """從 DB etf_daily_data 取 52 週最高/最低。
    優先用 day_high/day_low；若為 NULL 改用 current_price（TWSE 補齊後有值）。
    只取 current_price > 0 的有效紀錄，避免 0 值拉低 MIN。
    """
    try:
        from datetime import timedelta
        since = (date.today() - timedelta(days=370)).strftime("%Y-%m-%d")
        with get_db() as (conn, cursor):
            cursor.execute(
                """
                SELECT
                    MAX(COALESCE(NULLIF(day_high, 0), current_price))  AS h,
                    MIN(COALESCE(NULLIF(day_low,  0), current_price))  AS l
                FROM etf_daily_data
                WHERE ticker = %s AND date >= %s AND current_price > 0
                """,
                (ticker, since),
            )
            row = cursor.fetchone()
            if row:
                h = float(row["h"] or 0)
                l = float(row["l"] or 0)
                if h > 0 and l > 0:
                    # 盤中最新價也算進去（今日高點可能尚未入庫）
                    h = max(h, fallback_price)
                    l = min(l, fallback_price)
                    return round(h, 2), round(l, 2)
    except Exception as e:
        logger.debug(f"52W H/L DB {ticker}: {e}")
    return round(fallback_price * 1.15, 2), round(fallback_price * 0.85, 2)


def _fetch_yahoo_quotesummary(yt: str) -> dict:
    """Yahoo Finance v10 quoteSummary（含 crumb 認證，自動刷新）。"""
    global _yf_crumb, _yf_crumb_cookies
    empty = {"asset_size": 0.0, "pe_ratio": 0.0, "expense_ratio": 0.0, "nav": 0.0, "div_yield": 0.0}
    with _crumb_lock:
        has_crumb = bool(_yf_crumb)
    if not has_crumb:
        _refresh_yahoo_crumb()

    def _raw(d, key):
        v = d.get(key)
        if isinstance(v, dict): return safe_float(v.get("raw", 0))
        return safe_float(v)

    for attempt in range(3):
        try:
            with _crumb_lock:
                crumb = _yf_crumb
                cookies_snap = dict(_yf_crumb_cookies)
            url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{yt}"
                   f"?modules=fundProfile,summaryDetail,defaultKeyStatistics&crumb={crumb}")
            s = _new_session()
            s.headers["Referer"] = f"https://finance.yahoo.com/quote/{yt}"
            if cookies_snap:
                s.cookies.update(cookies_snap)
            r = s.get(url, timeout=6)
            if r.status_code == 401:
                with _crumb_lock:
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
            if attempt < 2: time.sleep(2 * (attempt + 1))
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
        # CF Proxy 優先（Railway IP 被 Yahoo 封鎖）→ 直連 fallback
        s = _new_session(referer)
        s.headers["Origin"] = "https://finance.yahoo.com"
        r = (_cf_yahoo_get(url, timeout=15)
             or _get_with_retry(s, url, timeout=6, max_attempts=3))
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

    div_yield, payout_freq, div_confirmed = _fetch_tw_dividend(ticker, price)
    history_closes = _fetch_tw_history(ticker)

    # ── 終點修正（Yahoo 有資料時才啟用）──
    # Yahoo 月線最後一筆 = 上個月末收盤，本月至今漲跌被漏掉。
    # 注意：只在 history_closes 足夠長時才覆蓋，避免資料太少時算出錯誤的近零報酬
    # 並繞過 IS NOT NULL 保護，覆蓋舊的正確值。
    if len(history_closes) >= 13 and price > 0:
        history_closes = list(history_closes)
        history_closes[-1] = price

    # 計算年化報酬需要「起點 + N 個月」共 N+1 筆，故 cutoff 用 -(N+1)
    # Railway 上 Yahoo 被封鎖 → history_closes = [] → ann_* = None
    # → IS NOT NULL 守衛保留 DB 舊值（不會被 None 覆蓋）
    cutoff_1y = len(history_closes) - 13 if len(history_closes) >= 13 else 0
    cutoff_3y = len(history_closes) - 37 if len(history_closes) >= 37 else 0
    ann_1y = _annualized_return(history_closes[cutoff_1y:], 1.0)
    ann_3y = _annualized_return(history_closes[cutoff_3y:], 3.0)
    ann_5y = _annualized_return(history_closes, 5.0)

    # ── 52 週最高/最低：優先從 DB 查真實 day_high/day_low ──
    wk52_h, wk52_l = _fetch_52week_hl_db(ticker, price)

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
        'expense_ratio': detail.get("expense_ratio") or KNOWN_EXPENSE_RATIO.get(ticker, 0),
        'dividend_yield': div_yield,
        'payout_freq': payout_freq or "不配息",
        'dividend_confirmed': div_confirmed,  # True=API 明確確認（可覆蓋 DB 即使值為 0）
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
    history, div_yield, payout_freq, div_confirmed = [], 0.0, "不配息", False
    try:
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=5y&interval=1mo&events=dividends"
        # CF Proxy 優先（Railway IP 被 Yahoo 封鎖）→ 直連 fallback（本機開發）
        r = (_cf_yahoo_get(url, timeout=15)
             or _get_with_retry(_new_session(f"https://finance.yahoo.com/quote/{ticker}"), url, timeout=6))
        if r and r.status_code == 200:
            res = r.json().get("chart", {}).get("result", [{}])[0]
            history = [safe_float(c) for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c is not None]
            events  = res.get("events", {}).get("dividends", {})
            div_confirmed = True  # Yahoo 成功回應，div_yield 值可信（即使為 0）
            if events:
                # 持久化全部 5 年配息事件到 etf_dividends（回測 DRIP 使用）
                all_ev = [
                    (ticker,
                     datetime.utcfromtimestamp(v["date"]).strftime("%Y-%m-%d"),
                     safe_float(v.get("amount", 0)))
                    for v in events.values()
                    if safe_float(v.get("amount", 0)) > 0
                ]
                _save_dividend_events(ticker, all_ev)

                cutoff = time.time() - 365 * 86400
                # amount > 0 過濾：與 TW 邏輯一致，排除零金額事件，防止膨脹頻率計數
                recent = [v["amount"] for v in events.values()
                          if v.get("date", 0) >= cutoff and safe_float(v.get("amount", 0)) > 0]
                if recent:
                    div_yield   = round(sum(recent) / price * 100, 4)
                    # 取 Yahoo 事件數 與 靜態備援 兩者中頻率等級較高者
                    payout_freq = _best_freq(len(recent), ticker)
    except Exception as e:
        logger.debug(f"US history/dividend {ticker}: {e}")

    # ── 終點修正 ──
    # Yahoo 月線最後一筆 = 上個月末收盤，本月至今的漲跌完全被漏掉。
    # 將最後一筆換成今日現價，確保年化報酬率算到「今日」。
    if history and price > 0:
        history = list(history)
        history[-1] = price

    # 計算年化報酬需要「起點 + N 個月」共 N+1 筆，故 cutoff 用 -(N+1)
    cutoff_1y = len(history) - 13 if len(history) >= 13 else 0
    cutoff_3y = len(history) - 37 if len(history) >= 37 else 0
    ann_1y = _annualized_return(history[cutoff_1y:], 1.0)
    ann_3y = _annualized_return(history[cutoff_3y:], 3.0)
    ann_5y = _annualized_return(history, 5.0)
    # 52週最高/最低：與 TW ETF 相同邏輯，優先查 DB 中真實的 day_high/day_low，
    # 比月線收盤更準確（月線只有月末收盤，無法反映月中極值）
    wk52_h, wk52_l = _fetch_52week_hl_db(ticker, price)

    detail = _fetch_yahoo_quotesummary(ticker)
    asset_size    = detail.get("asset_size", 0.0)
    pe_ratio      = detail.get("pe_ratio", 0.0)
    expense_ratio = detail.get("expense_ratio") or KNOWN_EXPENSE_RATIO.get(ticker, 0.0)
    nav           = detail.get("nav") or price
    yf_yield      = detail.get("div_yield", 0.0)
    if yf_yield > div_yield:
        div_yield = round(yf_yield, 4)
        if div_yield > 0 and payout_freq == "不配息":
            payout_freq = KNOWN_PAYOUT_FREQ.get(ticker, "季配")
    # 最終備援：若仍為「不配息」但靜態資料有記錄，以靜態資料為準
    if payout_freq == "不配息" and ticker in KNOWN_PAYOUT_FREQ:
        payout_freq = KNOWN_PAYOUT_FREQ[ticker]

    # 靜態殖利率備援：Yahoo 完全失敗（div_confirmed=False）且 yield=0 時、
    # 直接套用 KNOWN_YIELD_US 殖利率%，不依賴股價 → 股票分割/大漲後仍正確。
    # div_yield > 0 → save_etf_data 正常寫 DB（不受 confirmed=False 阻擋）
    if not div_confirmed and div_yield == 0.0 and ticker in KNOWN_YIELD_US:
        static_dy = KNOWN_YIELD_US[ticker]
        if static_dy > 0:
            div_yield = static_dy
            logger.debug(f"US 靜態殖利率備援 {ticker}: {div_yield:.2f}%")

    return {
        'ticker': ticker, 'current_price': price,
        'price_change': quote["price_change"],
        'price_change_percent': quote["price_change_percent"],
        'day_high': quote["day_high"], 'day_low': quote["day_low"],
        'fifty_two_week_high': wk52_h, 'fifty_two_week_low': wk52_l,
        'volume': quote["volume"], 'asset_size': asset_size, 'nav': nav,
        'pe_ratio': pe_ratio, 'expense_ratio': expense_ratio,
        'dividend_yield': div_yield, 'payout_freq': payout_freq,
        'dividend_confirmed': div_confirmed,  # True=Yahoo 明確確認（可覆蓋 DB 即使值為 0）
        'annual_return_1y': ann_1y,  # None = 資料不足（存 DB NULL，前端顯示「—」）
        'annual_return_3y': ann_3y,
        'annual_return_5y': ann_5y,
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
        dp  = round((cp - nav) / nav * 100, 2) if nav > 0 and abs(cp - nav) > 0.001 else 0.0

    # dividend_confirmed=True → API 已明確確認（yield=0 也可信，應覆蓋 DB）
    # dividend_confirmed=False → API 失敗，用靜態估算；yield=0 時傳 NULL 避免清空 DB 舊值
    div_confirmed = data.get("dividend_confirmed", False)
    raw_yield     = data.get("dividend_yield")
    dy_to_store   = raw_yield if (div_confirmed or (raw_yield and float(raw_yield) > 0)) else None

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
              -- price>0 才覆蓋：防止爬取瞬間失效（price=0）清除已有正確價格
              current_price=IF(VALUES(current_price)>0, VALUES(current_price), current_price),
              price_change=IF(VALUES(current_price)>0, VALUES(price_change), price_change),
              price_change_percent=VALUES(price_change_percent),
              volume=VALUES(volume), discount_premium=VALUES(discount_premium),
              -- 殖利率：IS NOT NULL 才更新
              --   confirmed=True  且 yield=0 → NULL→NULL，仍更新為 0（正確清零）
              --   confirmed=False 且 yield=0 → 傳 NULL，保留 DB 舊值（API 失敗保護）
              dividend_yield=IF(VALUES(dividend_yield) IS NOT NULL,
                                VALUES(dividend_yield),
                                COALESCE(dividend_yield,0)),
              -- 配息頻率：新值非「不配息」才更新（確保已知頻率不被清空）
              payout_freq=IF(VALUES(payout_freq)!='不配息',
                             VALUES(payout_freq),
                             COALESCE(payout_freq,'不配息')),
              -- 年化報酬：新值 IS NOT NULL 才更新（NULL = 資料不足，保留 DB 舊值）
              annual_return_1y=IF(VALUES(annual_return_1y) IS NOT NULL,
                                  VALUES(annual_return_1y),
                                  annual_return_1y),
              annual_return_3y=IF(VALUES(annual_return_3y) IS NOT NULL,
                                  VALUES(annual_return_3y),
                                  annual_return_3y),
              annual_return_5y=IF(VALUES(annual_return_5y) IS NOT NULL,
                                  VALUES(annual_return_5y),
                                  annual_return_5y),
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
            dy_to_store, data.get("payout_freq", "不配息"),
            data.get("annual_return_1y"),  # None → NULL（IS NOT NULL 邏輯區分「資料不足」與「計算值」）
            data.get("annual_return_3y"),
            data.get("annual_return_5y"),
            safe_float(data.get("pe_ratio")),
            safe_float(data.get("expense_ratio")),
            safe_float(data.get("day_high")),
            safe_float(data.get("day_low")),
            safe_float(data.get("fifty_two_week_high")),
            safe_float(data.get("fifty_two_week_low")),
        ))
        conn.commit()
    cache.delete(f"detail:{ticker}")
    market = data.get("market", "")
    if market:
        # combined 是前端主排行榜的快取 key，必須一起清除
        cache.delete(f"rank:combined:{market}")
        for rank_type in ("volume", "return", "yield"):
            cache.delete(f"rank:{rank_type}:{market}")
    else:
        cache.delete_prefix("rank:")


# ══════════════════════════════════════════════════════════
#  快速報價路徑（盤中高頻用，只抓現價，不碰 Yahoo 補充資料）
# ══════════════════════════════════════════════════════════

def fetch_price_only(ticker: str, market: str) -> Optional[dict]:
    """只抓現價，不抓 dividend/history/quoteSummary。
    TW 股走 TWSE 官方 API（穩定、毫秒級）；US 股走輕量 Yahoo chart（10d/1d）。
    用於盤中 2 分鐘快速更新，比完整 fetch 快 5-7 倍，Yahoo 請求量降 ~70%。
    """
    if market == "TW":
        quote = _fetch_tw_realtime_perfect(ticker)
        if not quote:
            return None
        return {
            "ticker": ticker, "market": market,
            "current_price": quote["current_price"],
            "price_change": quote["price_change"],
            "price_change_percent": quote["price_change_percent"],
            "day_high": quote["day_high"],
            "day_low": quote["day_low"],
            "volume": quote["volume"],
        }
    else:
        quote = _fetch_us_quote(ticker)
        if not quote:
            return None
        return {
            "ticker": ticker, "market": market,
            **quote,
        }


def save_price_only(data: dict):
    """只更新報價欄位（price/change/high/low/volume），保留 DB 中已有的補充資料。
    與 save_etf_data 的差異：不碰 dividend_yield、expense_ratio、annual_return 等欄位。
    """
    today  = datetime.now().date()
    ticker = data.get("ticker", "")
    cp     = safe_float(data.get("current_price"))

    with get_db() as (conn, cursor):
        cursor.execute("""
            INSERT INTO etf_daily_data
              (ticker, date, current_price, price_change, price_change_percent,
               day_high, day_low, volume)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE
              -- price>0 才覆蓋（save_price_only 同樣需要防零價守衛）
              current_price=IF(VALUES(current_price)>0, VALUES(current_price), current_price),
              price_change=IF(VALUES(current_price)>0, VALUES(price_change), price_change),
              price_change_percent=IF(VALUES(current_price)>0, VALUES(price_change_percent), price_change_percent),
              day_high=IF(VALUES(current_price)>0, VALUES(day_high), day_high),
              day_low=IF(VALUES(current_price)>0, VALUES(day_low), day_low),
              volume=IF(VALUES(current_price)>0, VALUES(volume), volume)
        """, (
            ticker, today, cp,
            safe_float(data.get("price_change")),
            safe_float(data.get("price_change_percent")),
            safe_float(data.get("day_high", cp)),
            safe_float(data.get("day_low", cp)),
            int(safe_float(data.get("volume", 0))),
        ))
        conn.commit()

    cache.delete(f"detail:{ticker}")
    market = data.get("market", "")
    if market:
        cache.delete(f"rank:combined:{market}")
        for rank_type in ("volume", "return", "yield"):
            cache.delete(f"rank:{rank_type}:{market}")
    else:
        cache.delete_prefix("rank:")


# ── 需要 pandas ──
try:
    import pandas as pd
except ImportError:
    pd = None
