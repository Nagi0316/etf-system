"""
etf_data.py — ETF 靜態清單、資料抓取、DB 存取
（從原 main.py 提取並整合 exchange_rate）
"""
from __future__ import annotations

import csv
import io
import logging
import os
import random
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote as _url_quote
from zoneinfo import ZoneInfo

import certifi
import requests as req_lib
import yfinance as yf

from cache import cache
from database import get_db
from utils import safe_float

logger = logging.getLogger(__name__)


def record_sync_state(dataset: str, market: str, expected_count: int,
                      quotes: dict, detail: str = "") -> None:
    """保存資料管線狀態，而不是用頁面請求時間假裝資料更新時間。

    每個資料集／市場只保留一列，避免高頻排程形成無限增長的稽核表。
    ``last_success_at`` 只在至少取得一筆有效資料時前進。
    """
    received_count = len(quotes)
    status = (
        "success" if expected_count > 0 and received_count >= expected_count
        else "partial" if received_count > 0
        else "failed"
    )
    quote_days = [_quote_date(q.get("quote_date")) for q in quotes.values()]
    data_day = max(quote_days) if quote_days else None
    now = datetime.now(ZoneInfo("Asia/Taipei")).replace(tzinfo=None)
    success_at = now if received_count > 0 else None
    try:
        with get_db() as (conn, cursor):
            cursor.execute(
                "SELECT data_date, last_success_at FROM data_sync_state "
                "WHERE dataset=%s AND market=%s",
                (dataset, market),
            )
            previous = cursor.fetchone()
            if previous:
                cursor.execute("""
                    UPDATE data_sync_state SET
                      expected_count=%s, received_count=%s, data_date=%s,
                      status=%s, detail=%s, last_attempt_at=%s,
                      last_success_at=%s
                    WHERE dataset=%s AND market=%s
                """, (
                    expected_count, received_count,
                    data_day or previous.get("data_date"), status, detail[:500], now,
                    success_at or previous.get("last_success_at"), dataset, market,
                ))
            else:
                cursor.execute("""
                    INSERT INTO data_sync_state
                      (dataset, market, expected_count, received_count, data_date,
                       status, detail, last_attempt_at, last_success_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    dataset, market, expected_count, received_count, data_day,
                    status, detail[:500], now, success_at,
                ))
            conn.commit()
        # 同步狀態本身也是 API 資料；即使價格沒有變，狀態與觀測時間仍已更新。
        cache.delete_prefix("rank:")
        cache.delete("health:data")
    except Exception as exc:
        logger.warning("record_sync_state %s/%s failed: %s", dataset, market, exc)


def _quote_date(value=None) -> date:
    """將資料源的交易時間轉成交易日；無有效來源時間才退回今天。"""
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and value > 0:
        try:
            return datetime.fromtimestamp(value).date()
        except (OSError, OverflowError, ValueError):
            pass
    if isinstance(value, str):
        raw = value.strip().replace("/", "-")
        for fmt in ("%Y-%m-%d", "%Y%m%d"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return date.today()

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

# ── 盤中快速報價 dirty-check 快取 ──
# {ticker: (price, volume)} — 上次寫入 DB 的值，用於跳過未變動的 ETF
# 只在 save_price_bulk 成功寫入後更新，不影響正確性
_price_dirty_cache: dict = {}

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
    '00685L': '不配息',  # 群益臺灣加權正2
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
    {'ticker': 'SPYM', 'name': 'State Street SPDR Portfolio S&P 500 ETF','market': 'US', 'hot': True, 'issuer': 'State Street', 'listing_date': '2005-11-08', 'category': 'broad_market'},
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

        # 已確認的代碼更名／歷史誤植：保留舊資料但從有效名錄排除。
        # SPLG 已於 2025-10-31 更名為 SPYM；其餘三筆是早期匯入時把
        # 發行商／基金名稱誤當 ticker，或把 TIP 錯植為 TIPS。
        cursor.execute(
            "UPDATE etf_master SET is_hot=0, is_delisted=1 "
            "WHERE ticker IN (%s,%s,%s,%s)",
            ("SPLG", "TIPS", "INVESCO", "VANGUARD S&P 500 ETF"),
        )

        # 已由產品性質確認不配息的台股槓桿／反向／商品 ETF：修復舊版
        # 抓取曾誤標成季配的歷史列。只處理明確白名單，不猜測其他商品。
        no_payout_tw = tuple(
            ticker for ticker, freq in KNOWN_PAYOUT_FREQ.items()
            if freq == "不配息" and ticker[:1].isdigit()
        )
        if no_payout_tw:
            fmt = ",".join(["%s"] * len(no_payout_tw))
            cursor.execute(
                f"UPDATE etf_daily_data "
                f"SET payout_freq='不配息', dividend_yield=0, "
                f"dividend_status='not_applicable' "
                f"WHERE ticker IN ({fmt})",
                no_payout_tw,
            )

        # SPLG→SPYM 是同一基金更名，歷史價格與配息必須延續，不能讓新代碼
        # 看起來像剛上市而失去年化報酬。重複執行安全，既有 SPYM 當日報價優先。
        cursor.execute("""
            INSERT INTO etf_daily_data
              (ticker,date,current_price,price_change,price_change_percent,volume,
               asset_size,nav,dividend_yield,payout_freq,
               annual_return_1y,annual_return_3y,annual_return_5y,
               pe_ratio,expense_ratio,day_high,day_low,
               fifty_two_week_high,fifty_two_week_low,discount_premium)
            SELECT
               'SPYM',old.date,old.current_price,old.price_change,
               old.price_change_percent,old.volume,
               old.asset_size,old.nav,old.dividend_yield,old.payout_freq,
               old.annual_return_1y,old.annual_return_3y,old.annual_return_5y,
               old.pe_ratio,old.expense_ratio,old.day_high,old.day_low,
               old.fifty_two_week_high,old.fifty_two_week_low,old.discount_premium
            FROM etf_daily_data AS old WHERE old.ticker='SPLG'
            ON DUPLICATE KEY UPDATE
               annual_return_1y=COALESCE(
                   etf_daily_data.annual_return_1y,VALUES(annual_return_1y)
               ),
               annual_return_3y=COALESCE(
                   etf_daily_data.annual_return_3y,VALUES(annual_return_3y)
               ),
               annual_return_5y=COALESCE(
                   etf_daily_data.annual_return_5y,VALUES(annual_return_5y)
               ),
               dividend_yield=COALESCE(
                   etf_daily_data.dividend_yield,VALUES(dividend_yield)
               ),
               expense_ratio=IF(
                   etf_daily_data.expense_ratio>0,
                   etf_daily_data.expense_ratio,
                   VALUES(expense_ratio)
               )
        """)
        cursor.execute("""
            INSERT IGNORE INTO etf_dividends (ticker,ex_date,amount,currency)
            SELECT 'SPYM',ex_date,amount,currency
            FROM etf_dividends WHERE ticker='SPLG'
        """)

        conn.commit()
    logger.info(f"✅ etf_master 種子資料完成（{len(ALL_ETFS)} 檔熱門 ETF，含 category）")


# ══════════════════════════════════════════════════════════
#  台股即時報價
# ══════════════════════════════════════════════════════════

def _parse_tw_mis_quote(d: dict) -> Optional[dict]:
    """解析 TWSE/TPEX MIS 報價，避免收盤後把昨收誤當今日收盤。

    MIS 在收盤撮合後的短暫時段可能令 ``z`` 為空，但 ``pz`` 已有最後
    成交價；``y`` 永遠是昨收。若今日已有成交卻拿不到 z/pz，寧可交給
    備援來源，也不能把昨收、今日成交量與今日高低拼成矛盾資料。
    """
    z_val = d.get("z", "-")
    pz_val = d.get("pz", "-")
    prev = safe_float(d.get("y", "0"))
    high = safe_float(d.get("h", "0"))
    low = safe_float(d.get("l", "0"))
    vol_k = safe_float(d.get("v", "0"))

    current = safe_float(z_val)
    used_fallback_last = False
    if current <= 0:
        current = safe_float(pz_val)
        used_fallback_last = current > 0

    has_session_activity = vol_k > 0 or high > 0 or low > 0
    if current <= 0:
        if has_session_activity:
            return None
        current = prev
    if current <= 0:
        return None

    change = round(current - prev, 4) if prev > 0 else 0.0
    return {
        "current_price": current,
        "price_change": change,
        "price_change_percent": (
            round(change / prev * 100, 4) if prev > 0 else 0.0
        ),
        "day_high": high or current,
        "day_low": low or current,
        "volume": int(vol_k * 1000),
        "is_after_hours": z_val in ("-", ""),
        "used_fallback_last": used_fallback_last,
        "quote_date": _quote_date(d.get("d")),
        "quote_source": "twse_mis",
    }


def _expected_tw_quote_day(now: datetime | None = None) -> date:
    current = now or datetime.now(ZoneInfo("Asia/Taipei"))
    candidate = current.date()
    if candidate.weekday() >= 5 or current.hour < 9:
        candidate -= timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _tw_market_session_open(now: datetime | None = None) -> bool:
    """Return whether the regular Taiwan session is currently trading."""
    current = now or datetime.now(ZoneInfo("Asia/Taipei"))
    return (
        current.weekday() < 5
        and (9, 0) <= (current.hour, current.minute) <= (13, 30)
    )


def _tw_quote_is_fresh(quote: dict, expected: date | None = None) -> bool:
    quote_day = quote.get("quote_date")
    if isinstance(quote_day, str):
        try:
            quote_day = date.fromisoformat(quote_day[:10])
        except ValueError:
            return False
    return isinstance(quote_day, date) and quote_day >= (expected or _expected_tw_quote_day())


def _fetch_tw_realtime_perfect(ticker: str) -> Optional[dict]:
    """台股即時報價。

    優先使用 TWSE／TPEX MIS；若 Railway 出口 IP 無法連線官方 MIS，
    改走 Yahoo chart（Cloudflare Worker 優先、直連次之）。
    """
    for prefix, base_url, referer in [
        ("tse", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",  "https://mis.twse.com.tw/"),
        # TWSE MIS 同時代理 otc_ 報價；此網域比 mis.tpex.org.tw 的
        # HTML/逾時回應穩定，且仍是交易所官方來源。
        ("otc", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",  "https://mis.twse.com.tw/"),
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
            parsed = _parse_tw_mis_quote(d)
            if not parsed:
                continue
            # 部分雲端出口取得的 MIS 盤後回應只有現價／昨收，日高低與成交量為空。
            # 以 Yahoo 補齊缺失欄位，避免新日期列把完整詳情變成 0。
            if (parsed["volume"] <= 0 or parsed["day_high"] <= 0
                    or parsed["day_low"] <= 0):
                fallback = _fetch_tw_yahoo_quote(ticker)
                if fallback:
                    return fallback
            return parsed
        except Exception as e:
            logger.debug(f"TW realtime {prefix} {ticker}: {e}")
    quote = _fetch_tw_yahoo_quote(ticker)
    if quote:
        logger.info(f"TW realtime {ticker}: MIS unavailable, using Yahoo fallback")
    return quote


def _fetch_tw_yahoo_quote(ticker: str) -> Optional[dict]:
    """從 Yahoo chart 取得單一台股 ETF 報價，作為 MIS 的免費備援。

    使用日線而非分鐘線，回應較小且能取得現價、前收、日高低與成交量。
    結果短暫快取，避免 MIS 故障時每 30 秒對同一代碼重複請求。
    """
    cache_key = f"tw_yahoo_quote:{ticker}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    primary = _yahoo_ticker(ticker, "TW")
    symbols = [primary]
    alt = f"{ticker}.TWO" if primary.endswith(".TW") else f"{ticker}.TW"
    if alt not in symbols:
        symbols.append(alt)

    for symbol in symbols:
        url = (
            f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
            "?range=10d&interval=1d"
        )
        try:
            session = _new_session(f"https://finance.yahoo.com/quote/{symbol}")
            session.headers["Origin"] = "https://finance.yahoo.com"
            response = (
                _cf_yahoo_get(url, timeout=15)
                or _get_with_retry(session, url, timeout=8, max_attempts=1)
            )
            if not response or response.status_code != 200:
                continue

            results = response.json().get("chart", {}).get("result")
            if not results:
                continue

            result = results[0]
            meta = result.get("meta", {})
            quote = result.get("indicators", {}).get("quote", [{}])[0]
            timestamps = result.get("timestamp") or []
            closes = [safe_float(v) for v in (quote.get("close") or []) if v is not None]
            highs = [safe_float(v) for v in (quote.get("high") or []) if v is not None]
            lows = [safe_float(v) for v in (quote.get("low") or []) if v is not None]
            volumes = [safe_float(v) for v in (quote.get("volume") or []) if v is not None]

            price = closes[-1] if closes else safe_float(meta.get("regularMarketPrice"))
            if price <= 0:
                continue
            previous = (
                closes[-2] if len(closes) >= 2
                else safe_float(meta.get("chartPreviousClose"), price)
            )
            change = round(price - previous, 4) if previous > 0 else 0.0
            parsed = {
                "current_price": price,
                "price_change": change,
                "price_change_percent": (
                    round(change / previous * 100, 4) if previous > 0 else 0.0
                ),
                "day_high": highs[-1] if highs and highs[-1] > 0 else price,
                "day_low": lows[-1] if lows and lows[-1] > 0 else price,
                "volume": int(volumes[-1]) if volumes else 0,
                "is_after_hours": False,
                "quote_date": _quote_date(
                    timestamps[-1] if timestamps else meta.get("regularMarketTime")
                ),
                "quote_source": "yahoo_chart",
            }
            cache.set(cache_key, parsed, ttl=45)
            return parsed
        except Exception as e:
            logger.debug(f"TW Yahoo fallback {symbol}: {e}")
    return None


def _parse_tw_market_date(raw) -> date:
    """解析證交所／櫃買中心的民國年月日（例如 1150727）。"""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 7:
        try:
            return date(int(digits[:3]) + 1911, int(digits[3:5]), int(digits[5:7]))
        except ValueError:
            pass
    return _quote_date(raw)


def _fetch_tw_official_bulk() -> dict:
    """從 TWSE/TPEX OpenAPI 一次取得全市場最近交易日收盤。

    MIS 適合盤中即時價，但雲端出口偶爾連線不穩；官方 OpenAPI 回傳整個
    上市／上櫃市場，可在兩次請求內穩定補齊價格、成交量與日高低。
    """
    sources = (
        (
            "TWSE",
            "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            {
                "ticker": "Code", "date": "Date", "price": "ClosingPrice",
                "change": "Change", "high": "HighestPrice", "low": "LowestPrice",
                "volume": "TradeVolume",
            },
            "tw_official_openapi",
        ),
        (
            "TWSE",
            "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=open_data",
            {
                "ticker": "證券代號", "date": "日期", "price": "收盤價",
                "change": "漲跌價差", "high": "最高價", "low": "最低價",
                "volume": "成交股數",
            },
            "tw_official_csv",
        ),
        (
            "TPEX",
            "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes",
            {
                "ticker": "SecuritiesCompanyCode", "date": "Date", "price": "Close",
                "change": "Change", "high": "High", "low": "Low",
                "volume": "TradingShares",
            },
            "tw_official_openapi",
        ),
    )
    result: dict = {}
    headers = {
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
    }
    completed_groups: set[str] = set()
    for group, url, fields, source_name in sources:
        # TWSE OpenAPI 偶爾重置雲端連線；只在主端點失敗時
        # 改讀同一日報的官方 CSV，避免每輪多打一次請求。
        if group in completed_groups:
            continue
        try:
            response = req_lib.get(url, headers=headers, timeout=15)
            response.raise_for_status()
            try:
                rows = response.json()
            except ValueError:
                decoded = None
                for encoding in ("utf-8-sig", "cp950", "utf-8"):
                    try:
                        decoded = response.content.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
                rows = list(csv.DictReader(io.StringIO(decoded or "")))

            source_count = 0
            for item in rows:
                ticker = str(item.get(fields["ticker"], "")).strip().upper()
                price = safe_float(item.get(fields["price"]))
                if not ticker or price <= 0:
                    continue
                change = safe_float(item.get(fields["change"]))
                previous = price - change
                result[ticker] = {
                    "current_price": price,
                    "price_change": round(change, 4),
                    "price_change_percent": (
                        round(change / previous * 100, 4) if previous > 0 else 0.0
                    ),
                    "day_high": safe_float(item.get(fields["high"])) or price,
                    "day_low": safe_float(item.get(fields["low"])) or price,
                    "volume": int(safe_float(item.get(fields["volume"]))),
                    "is_after_hours": True,
                    "quote_date": _parse_tw_market_date(item.get(fields["date"])),
                    "quote_source": source_name,
                }
                source_count += 1
            if source_count:
                completed_groups.add(group)
        except Exception as e:
            logger.warning(f"TW official bulk source failed ({url}): {e}")
    return result


def _fetch_tw_official_month_quote(ticker: str) -> Optional[dict]:
    """補抓不在全市場快照中的特殊 ETF（外幣加掛、期貨槓反等）。"""
    today = date.today()
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
    try:
        response = req_lib.get(
            url,
            params={
                "stockNo": ticker,
                "date": f"{today.year}{today.month:02d}01",
                "response": "json",
            },
            headers={
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
            },
            timeout=12,
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
        valid = []
        for row in rows:
            if len(row) < 7:
                continue
            close = safe_float(str(row[6]).replace(",", ""))
            if close > 0:
                valid.append((row, close))
        if not valid:
            return None

        row, price = valid[-1]
        previous = valid[-2][1] if len(valid) >= 2 else price
        change = round(price - previous, 4)
        tw_date = str(row[0]).strip().split("/")
        quote_date = date(int(tw_date[0]) + 1911, int(tw_date[1]), int(tw_date[2]))
        return {
            "current_price": price,
            "price_change": change,
            "price_change_percent": (
                round(change / previous * 100, 4) if previous > 0 else 0.0
            ),
            "day_high": safe_float(str(row[4]).replace(",", "")) or price,
            "day_low": safe_float(str(row[5]).replace(",", "")) or price,
            "volume": int(safe_float(str(row[1]).replace(",", ""))),
            "is_after_hours": True,
            "quote_date": quote_date,
            "quote_source": "twse_monthly",
        }
    except Exception as e:
        logger.debug(f"TW official monthly quote {ticker}: {e}")
        return None


def _fetch_tw_realtime_bulk(tickers: list) -> dict:
    """TWSE / TPEX 批量即時報價。

    策略：
      1. tse / otc MIS 以每批 100 檔並行查詢。
      2. 官方全市場日報補齊 MIS 未提供的欄位。
      3. 盤中全市場模式不啟動逐檔慢速備援，避免 30 秒任務
         卡住數分鐘；單檔詳情頁與收盤後流程仍會完整補抓。

    回傳: {ticker: quote_dict}，只包含成功取得資料的標的。
    失敗標的不在回傳 dict 中，呼叫方以 .get(ticker) 安全存取。
    """
    if not tickers:
        return {}
    expected_day = _expected_tw_quote_day()

    def _batch_request(prefix: str, batch_tickers: list,
                       base_url: str, referer: str) -> dict:
        """向 TWSE/TPEX MIS API 批量查詢，回傳 {ticker: quote_dict}。"""
        # 每批上限 100 檔，避免 URL 過長或 API 回拒。
        # 各批互不依賴，並行請求可將最壞等待限制在單批逾時內。
        import concurrent.futures

        chunk_size = 100
        batch_result: dict = {}
        chunks = [
            batch_tickers[i:i + chunk_size]
            for i in range(0, len(batch_tickers), chunk_size)
        ]

        def _request_chunk(chunk: list) -> dict:
            chunk_result: dict = {}
            ex_ch = "|".join(f"{prefix}_{t}.tw" for t in chunk)
            url   = f"{base_url}?ex_ch={ex_ch}&json=1&delay=0"
            try:
                s = _new_session(referer)
                r = _get_with_retry(s, url, timeout=8, max_attempts=2)
                if not r:
                    return chunk_result
                items = r.json().get("msgArray", [])
                for d in items:
                    t = d.get("c", "")          # TWSE 回傳的股票代碼（純數字/英文）
                    if not t:
                        continue
                    parsed = _parse_tw_mis_quote(d)
                    if not parsed or not _tw_quote_is_fresh(parsed, expected_day):
                        continue
                    chunk_result[t] = parsed
            except Exception as e:
                logger.debug(f"TW bulk realtime {prefix}: {e}")
            return chunk_result

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(4, len(chunks))
        ) as pool:
            for chunk_result in pool.map(_request_chunk, chunks):
                batch_result.update(chunk_result)
        return batch_result

    # ─ Pass 1：TSE 前綴（TWSE 上市，含大多數 ETF）─
    result = _batch_request(
        "tse", tickers,
        "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
        "https://mis.twse.com.tw/",
    )

    # ─ Pass 2：OTC 前綴（TPEX 上櫃，首次未命中的才重查）─
    missing = [t for t in tickers if t not in result]
    if missing:
        otc_result = _batch_request(
            "otc", missing,
            "https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
            "https://mis.twse.com.tw/",
        )
        result.update(otc_result)

    # ─ Pass 3：TWSE/TPEX 官方全市場 OpenAPI 備援 ─
    # 固定兩次請求即可覆蓋上市與上櫃，避免 MIS 不穩時退化成數百次 Yahoo 請求。
    incomplete = {
        t for t in tickers
        if (
            t not in result
            or result[t].get("volume", 0) <= 0
            or result[t].get("day_high", 0) <= 0
            or result[t].get("day_low", 0) <= 0
        )
    }
    if incomplete:
        official = _fetch_tw_official_bulk()
        for ticker in incomplete:
            if ticker in official and _tw_quote_is_fresh(official[ticker], expected_day):
                result[ticker] = official[ticker]

    # ─ Pass 4：官方單檔月成交備援 ─
    # 外幣加掛、期貨槓反等少數商品不一定出現在 STOCK_DAY_ALL。
    remaining = [
        ticker for ticker in tickers
        if ticker not in result or result[ticker].get("current_price", 0) <= 0
    ]
    allow_slow_fallbacks = (
        len(tickers) <= 80 or not _tw_market_session_open()
    )
    if remaining and allow_slow_fallbacks:
        # Railway 可能擋住 MIS，若逐檔串行抓 40 檔，最壞會卡 8 分鐘且
        # 整批結束前一筆都不會寫入。有限並發把收盤後修復壓到數十秒內。
        import concurrent.futures

        monthly_targets = remaining[:40]
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(12, len(monthly_targets))
        ) as pool:
            future_to_ticker = {
                pool.submit(_fetch_tw_official_month_quote, ticker): ticker
                for ticker in monthly_targets
            }
            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    quote = future.result()
                    if quote and _tw_quote_is_fresh(quote, expected_day):
                        result[ticker] = quote
                except Exception as e:
                    logger.debug(f"TW monthly bulk fallback {ticker}: {e}")

    # ─ Pass 5：Yahoo chart 最終備援 ─
    # Railway 的出口 IP 可能無法連線 MIS。只針對前兩輪未命中的代碼，
    # 以有限並發透過 Cloudflare Worker 抓取，避免單一來源故障讓全台股停更。
    fallback_targets = [
        t for t in tickers
        if (
            t not in result
            or result[t].get("volume", 0) <= 0
            or result[t].get("day_high", 0) <= 0
            or result[t].get("day_low", 0) <= 0
        )
    ]

    # 先用 Yahoo v7 批量補齊（每檔同時帶 .TW / .TWO，由回應決定有效市場）。
    # 這條路徑在 Railway 可走選配代理，也可直連；成功時僅需 1-2 次請求，
    # 避免 MIS 暫時不可用時退化成數十檔逐筆請求。
    if fallback_targets and allow_slow_fallbacks:
        symbol_to_ticker = {}
        yahoo_symbols = []
        for ticker in fallback_targets:
            for symbol in (f"{ticker}.TW", f"{ticker}.TWO"):
                yahoo_symbols.append(symbol)
                symbol_to_ticker[symbol] = ticker

        for i in range(0, len(yahoo_symbols), 120):
            chunk = yahoo_symbols[i:i + 120]
            url = (
                "https://query2.finance.yahoo.com/v7/finance/quote"
                f"?symbols={','.join(chunk)}"
                "&fields=regularMarketPrice,regularMarketChange,"
                "regularMarketChangePercent,regularMarketDayHigh,"
                "regularMarketDayLow,regularMarketVolume,regularMarketTime"
            )
            try:
                response = (
                    _cf_yahoo_get(url, timeout=15)
                    or _get_with_retry(
                        _new_session("https://finance.yahoo.com/markets/etfs/"),
                        url, timeout=8, max_attempts=1,
                    )
                )
                if not response or response.status_code != 200:
                    continue
                for item in response.json().get("quoteResponse", {}).get("result", []):
                    ticker = symbol_to_ticker.get(item.get("symbol", ""))
                    price = safe_float(item.get("regularMarketPrice", 0))
                    if not ticker or price <= 0:
                        continue
                    candidate = {
                        "current_price": price,
                        "price_change": round(safe_float(item.get("regularMarketChange")), 4),
                        "price_change_percent": round(
                            safe_float(item.get("regularMarketChangePercent")), 4
                        ),
                        "day_high": safe_float(item.get("regularMarketDayHigh")) or price,
                        "day_low": safe_float(item.get("regularMarketDayLow")) or price,
                        "volume": int(safe_float(item.get("regularMarketVolume"))),
                        "is_after_hours": False,
                        "quote_date": _quote_date(item.get("regularMarketTime")),
                        "quote_source": "yahoo_quote",
                    }
                    if not _tw_quote_is_fresh(candidate, expected_day):
                        continue
                    # 第一個有效市場即足夠；避免無效替代代碼覆蓋正式市場。
                    result.setdefault(ticker, candidate)
                    if (result[ticker].get("volume", 0) <= 0 or
                            result[ticker].get("day_high", 0) <= 0):
                        result[ticker] = candidate
            except Exception as e:
                logger.debug(f"TW Yahoo bulk fallback chunk {i}: {e}")

        fallback_targets = [
            t for t in fallback_targets
            if (
                t not in result
                or result[t].get("volume", 0) <= 0
                or result[t].get("day_high", 0) <= 0
                or result[t].get("day_low", 0) <= 0
            )
        ]

    if fallback_targets and allow_slow_fallbacks:
        import concurrent.futures

        # 上游同時故障時設硬上限，避免 355 檔逐筆 timeout 讓整輪排程卡死。
        fallback_targets = fallback_targets[:24]
        max_workers = min(12, len(fallback_targets))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_ticker = {
                pool.submit(_fetch_tw_yahoo_quote, ticker): ticker
                for ticker in fallback_targets
            }
            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    quote = future.result()
                    if quote and _tw_quote_is_fresh(quote, expected_day):
                        result[ticker] = quote
                except Exception as e:
                    logger.debug(f"TW bulk Yahoo fallback {ticker}: {e}")

    logger.debug(
        f"TW bulk realtime: 請求 {len(tickers)} 檔，命中 {len(result)} 檔，"
        f"未命中 {len(tickers) - len(result)} 檔"
    )
    return result


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
                    # Yahoo 成功但沒有事件，不代表已知配息 ETF 真的停止配息；
                    # Railway／代理偶爾會收到缺少 events 的部分回應，應繼續嘗試
                    # 替代代碼、TWSE 與靜態備援，避免把既有殖利率錯誤清成 0。
                    freq = KNOWN_PAYOUT_FREQ.get(ticker, "不配息")
                    if freq == "不配息":
                        return 0.0, freq, True
                    continue
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

def _fetch_us_quote(ticker: str, fast: bool = False) -> Optional[dict]:
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?range=10d&interval=1d"
    referer = f"https://finance.yahoo.com/quote/{ticker}"
    try:
        # CF Proxy 優先（Railway IP 被 Yahoo 封鎖）→ 直連 fallback
        s = _new_session(referer)
        s.headers["Origin"] = "https://finance.yahoo.com"
        r = (
            _cf_yahoo_get(url, timeout=6 if fast else 15)
            or _get_with_retry(
                s,
                url,
                timeout=4 if fast else 6,
                max_attempts=1 if fast else 3,
            )
        )
        if not r or r.status_code != 200:
            return None
        j = r.json()
        result = j.get("chart", {}).get("result")
        if not result:
            return None
        meta    = result[0].get("meta", {})
        q       = result[0].get("indicators", {}).get("quote", [{}])[0]
        timestamps = result[0].get("timestamp") or []
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
            "quote_date": _quote_date(
                timestamps[-1] if timestamps else meta.get("regularMarketTime")
            ),
            "quote_source": "yahoo_chart",
        }
    except Exception as e:
        logger.debug(f"US quote {ticker}: {e}")
    return None


def _fetch_us_nasdaq_quote(ticker: str) -> Optional[dict]:
    """從 Nasdaq 官方歷史報價取得最近兩個交易日，作為 Yahoo 備援。"""
    from datetime import timedelta

    today = date.today()
    url = f"https://api.nasdaq.com/api/quote/{ticker}/historical"
    try:
        response = req_lib.get(
            url,
            params={
                "assetclass": "etf",
                "fromdate": (today - timedelta(days=14)).isoformat(),
                "todate": today.isoformat(),
                "limit": 10,
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": f"https://www.nasdaq.com/market-activity/etf/{ticker.lower()}",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
                ),
            },
            timeout=12,
        )
        response.raise_for_status()
        rows = (
            response.json().get("data", {}).get("tradesTable", {}).get("rows")
            or []
        )
        valid = []
        for row in rows:
            price = safe_float(str(row.get("close", "")).replace(",", "").replace("$", ""))
            if price > 0:
                valid.append((row, price))
        if not valid:
            return None

        row, price = valid[0]  # Nasdaq 由新到舊排列
        previous = valid[1][1] if len(valid) >= 2 else price
        change = round(price - previous, 4)
        quote_date = datetime.strptime(row["date"], "%m/%d/%Y").date()
        return {
            "current_price": price,
            "price_change": change,
            "price_change_percent": (
                round(change / previous * 100, 4) if previous > 0 else 0.0
            ),
            "day_high": safe_float(str(row.get("high", "")).replace(",", "").replace("$", "")) or price,
            "day_low": safe_float(str(row.get("low", "")).replace(",", "").replace("$", "")) or price,
            "volume": int(safe_float(str(row.get("volume", "")).replace(",", ""))),
            "quote_date": quote_date,
            "quote_source": "nasdaq_history",
        }
    except Exception as e:
        logger.debug(f"Nasdaq quote {ticker}: {e}")
        return None


def _fetch_us_realtime_bulk(tickers: list) -> dict:
    """Yahoo Finance v7 /quote 批量現價：一次 HTTP 請求取得所有美股 ETF 現價。

    使用 /v7/finance/quote?symbols=SPY,QQQ,VOO 端點（比 v8/chart 更輕量）。
    CF Proxy 優先 → 直連 fallback（Railway IP 可能被 Yahoo 封鎖）。

    回傳: {ticker: quote_dict}，只包含成功取得資料的標的。
    """
    if not tickers:
        return {}

    result: dict = {}
    # Yahoo v7 quote 端點支援最多約 1500 個 symbol，實務上分批 200 個避免 URL 過長
    chunk_size = 200
    for i in range(0, len(tickers), chunk_size):
        chunk   = tickers[i:i + chunk_size]
        symbols = ",".join(chunk)
        url     = (f"https://query2.finance.yahoo.com/v7/finance/quote"
                   f"?symbols={symbols}&fields=regularMarketPrice,"
                   f"regularMarketChange,regularMarketChangePercent,"
                   f"regularMarketDayHigh,regularMarketDayLow,"
                   f"regularMarketVolume,regularMarketPreviousClose,"
                   f"regularMarketTime")
        referer = "https://finance.yahoo.com/markets/etfs/most-active/"
        try:
            s = _new_session(referer)
            s.headers["Origin"] = "https://finance.yahoo.com"
            r = (_cf_yahoo_get(url, timeout=15)
                 or _get_with_retry(s, url, timeout=8, max_attempts=2))
            if not r or r.status_code != 200:
                logger.debug(f"US bulk realtime: HTTP {getattr(r,'status_code','None')}")
                continue
            items = r.json().get("quoteResponse", {}).get("result", [])
            for item in items:
                ticker = item.get("symbol", "")
                if not ticker:
                    continue
                price = safe_float(item.get("regularMarketPrice", 0))
                if price <= 0:
                    continue
                chg     = safe_float(item.get("regularMarketChange", 0))
                chg_pct = safe_float(item.get("regularMarketChangePercent", 0))
                result[ticker] = {
                    "current_price":        price,
                    "price_change":         round(chg, 4),
                    "price_change_percent": round(chg_pct, 4),
                    "day_high":  safe_float(item.get("regularMarketDayHigh",  price)),
                    "day_low":   safe_float(item.get("regularMarketDayLow",   price)),
                    "volume":    int(safe_float(item.get("regularMarketVolume", 0))),
                    "quote_date": _quote_date(item.get("regularMarketTime")),
                    "quote_source": "yahoo_quote",
                }
        except Exception as e:
            logger.debug(f"US bulk realtime chunk {i}: {e}")

    # Yahoo v7 quote 在部分出口會回 401；先以 Nasdaq 官方歷史報價補齊。
    missing = [ticker for ticker in tickers if ticker not in result]
    if missing:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(12, len(missing))
        ) as pool:
            future_to_ticker = {
                pool.submit(_fetch_us_nasdaq_quote, ticker): ticker
                for ticker in missing
            }
            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    quote = future.result()
                    if quote:
                        result[ticker] = quote
                except Exception as e:
                    logger.debug(f"US Nasdaq bulk fallback {ticker}: {e}")

    # Nasdaq 未收錄或暫時失敗時，最後再走 Yahoo v8 chart／CF proxy。
    missing = [ticker for ticker in tickers if ticker not in result]
    if missing:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(12, len(missing))
        ) as pool:
            future_to_ticker = {
                pool.submit(_fetch_us_quote, ticker, True): ticker
                for ticker in missing
            }
            for future in concurrent.futures.as_completed(future_to_ticker):
                ticker = future_to_ticker[future]
                try:
                    quote = future.result()
                    if quote:
                        result[ticker] = quote
                except Exception as e:
                    logger.debug(f"US Yahoo chart fallback {ticker}: {e}")

    logger.debug(
        f"US bulk realtime: 請求 {len(tickers)} 檔，命中 {len(result)} 檔，"
        f"未命中 {len(tickers) - len(result)} 檔"
    )
    return result


# ══════════════════════════════════════════════════════════
#  主抓取入口
# ══════════════════════════════════════════════════════════

def fetch_one_etf(ticker: str, market: str) -> Optional[dict]:
    if market == "TW":
        return _fetch_tw_etf(ticker)
    return _fetch_us_etf(ticker)


def _fetch_us_history_dividend(ticker: str, current_price: float) -> tuple:
    """Fetch US monthly closes and trailing-12-month distributions in one call."""
    history: list[float] = []
    dividend_yield = 0.0
    payout_freq = "不配息"
    confirmed = False
    try:
        url = (
            f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
            "?range=5y&interval=1mo&events=dividends"
        )
        response = (
            _cf_yahoo_get(url, timeout=15)
            or _get_with_retry(
                _new_session(f"https://finance.yahoo.com/quote/{ticker}"),
                url,
                timeout=6,
            )
        )
        if response and response.status_code == 200:
            result = response.json().get("chart", {}).get("result", [{}])[0]
            history = [
                safe_float(value)
                for value in (
                    result.get("indicators", {})
                    .get("quote", [{}])[0]
                    .get("close") or []
                )
                if value is not None
            ]
            events = result.get("events", {}).get("dividends", {})
            confirmed = True
            if events:
                all_events = [
                    (
                        ticker,
                        datetime.utcfromtimestamp(value["date"]).strftime("%Y-%m-%d"),
                        safe_float(value.get("amount", 0)),
                    )
                    for value in events.values()
                    if safe_float(value.get("amount", 0)) > 0
                ]
                _save_dividend_events(ticker, all_events)
                cutoff = time.time() - 365 * 86400
                recent = [
                    value["amount"] for value in events.values()
                    if value.get("date", 0) >= cutoff
                    and safe_float(value.get("amount", 0)) > 0
                ]
                if recent and current_price > 0:
                    dividend_yield = round(sum(recent) / current_price * 100, 4)
                    payout_freq = _best_freq(len(recent), ticker)
    except Exception as exc:
        logger.debug("US history/dividend %s: %s", ticker, exc)

    if payout_freq == "不配息" and ticker in KNOWN_PAYOUT_FREQ:
        payout_freq = KNOWN_PAYOUT_FREQ[ticker]
    if not confirmed and dividend_yield == 0.0:
        dividend_yield = KNOWN_YIELD_US.get(ticker, 0.0)
    return history, dividend_yield, payout_freq, confirmed


def fetch_dividend_only(ticker: str, market: str, current_price: float) -> dict:
    """低頻補齊專用：只抓配息，避免為全市場重抓歷史與基金詳情。"""
    if current_price <= 0:
        return {"dividend_status": "unknown"}
    if market == "TW":
        value, frequency, confirmed = _fetch_tw_dividend(ticker, current_price)
    elif market == "US":
        _, value, frequency, confirmed = _fetch_us_history_dividend(
            ticker, current_price
        )
    else:
        return {"dividend_status": "unknown"}
    return {
        "dividend_yield": value if value > 0 else None,
        "payout_freq": frequency,
        "dividend_confirmed": confirmed,
        "dividend_status": _resolved_dividend_status(
            value, frequency, confirmed, ticker
        ),
    }


def save_dividend_snapshot(ticker: str, quote_date, data: dict) -> bool:
    """只更新指定交易日的配息欄位；未知結果不覆蓋既有可信資料。"""
    status = data.get("dividend_status") or "unknown"
    if status == "unknown":
        return False
    with get_db() as (conn, cursor):
        cursor.execute("""
            UPDATE etf_daily_data SET
              dividend_yield=%s, payout_freq=%s, dividend_status=%s
            WHERE ticker=%s AND date=%s
        """, (
            data.get("dividend_yield"), data.get("payout_freq"), status,
            ticker, _quote_date(quote_date),
        ))
        changed = cursor.rowcount > 0
        conn.commit()
    if changed:
        cache.delete(f"detail:{ticker}")
        cache.delete_prefix("rank:")
        cache.delete("health:data")
    return changed


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

    # MIS 解析器已依本日最後成交價與昨收計算漲跌；不可在真正平盤時拿
    # 前一交易日的漲跌覆蓋，否則會把歷史變動誤標成今日變動。
    price_change     = quote["price_change"]
    price_change_pct = quote["price_change_percent"]

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
        'dividend_status': _resolved_dividend_status(
            div_yield, payout_freq, div_confirmed, ticker
        ),
        'annual_return_1y': ann_1y,
        'annual_return_3y': ann_3y,
        'annual_return_5y': ann_5y,
        'quote_date': quote.get("quote_date"),
    }


def _fetch_us_etf(ticker: str) -> Optional[dict]:
    quote = _fetch_us_quote(ticker)
    if not quote:
        try:
            df = yf.download(ticker, period="10d", interval="1d", progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 2:
                if getattr(df.columns, "nlevels", 1) > 1:
                    df.columns = df.columns.get_level_values(0)
                price = float(df['Close'].iloc[-1])
                prev  = float(df['Close'].iloc[-2])
                chg   = round(price - prev, 4)
                quote = {"current_price": price, "price_change": chg,
                         "price_change_percent": round(chg/prev*100, 4) if prev > 0 else 0,
                         "day_high": float(df['High'].iloc[-1]),
                         "day_low":  float(df['Low'].iloc[-1]),
                         "volume":   int(df['Volume'].iloc[-1]),
                         "quote_date": _quote_date(df.index[-1].date())}
        except Exception as e:
            logger.debug(f"US yf fallback {ticker}: {e}")
    if not quote:
        return None

    price = quote["current_price"]
    history, div_yield, payout_freq, div_confirmed = _fetch_us_history_dividend(
        ticker, price
    )

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
        'dividend_status': _resolved_dividend_status(
            div_yield, payout_freq, div_confirmed, ticker
        ),
        'annual_return_1y': ann_1y,  # None = 資料不足（存 DB NULL，前端顯示「—」）
        'annual_return_3y': ann_3y,
        'annual_return_5y': ann_5y,
        'quote_date': quote.get("quote_date"),
    }


# ══════════════════════════════════════════════════════════
#  存入 DB
# ══════════════════════════════════════════════════════════

def _resolved_payout_freq(data_freq: str | None, previous_freq: str | None,
                          confirmed: bool) -> str:
    incoming = data_freq or "不配息"
    if incoming == "不配息" and not confirmed and previous_freq:
        return previous_freq
    return incoming


def _resolved_dividend_status(dividend_yield, payout_freq: str | None,
                              confirmed: bool, ticker: str = "") -> str:
    """區分有數值、確定不配息、估算與尚未取得，避免全部顯示成破折號。"""
    value = safe_float(dividend_yield)
    if value > 0:
        return "confirmed" if confirmed else "estimated"
    if confirmed and payout_freq == "不配息":
        return "not_applicable"
    if payout_freq == "不配息" and KNOWN_PAYOUT_FREQ.get(ticker) == "不配息":
        return "not_applicable"
    return "unknown"


def save_etf_data(data: dict):
    today  = _quote_date(data.get("quote_date"))
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
        # 新交易日首次 INSERT 時，抓取上一筆補充欄位作為 carry-forward。
        # 快速報價／部分 API 可能只帶現價；若直接新增稀疏列，詳情頁讀最新日期後
        # 會把年化報酬、殖利率、費用率等顯示成空白。
        cursor.execute("""
            SELECT asset_size, nav, discount_premium,
                   dividend_yield, payout_freq,
                   annual_return_1y, annual_return_3y, annual_return_5y,
                   pe_ratio, expense_ratio,
                   fifty_two_week_high, fifty_two_week_low
            FROM etf_daily_data
            WHERE ticker=%s AND date < %s
            ORDER BY date DESC
            LIMIT 1
        """, (ticker, today))
        previous = cursor.fetchone() or {}

        if nav is None:
            prev_nav = safe_float(previous.get("nav"))
            nav = prev_nav if prev_nav > 0 else None
            dp = safe_float(previous.get("discount_premium"))
        if dy_to_store is None:
            prev_yield = previous.get("dividend_yield")
            dy_to_store = prev_yield if prev_yield is not None else None

        # 已確認無配息時必須允許把舊的錯誤頻率改回「不配息」；只有資料源
        # 未確認時，才沿用上一筆已知頻率。
        payout_freq = _resolved_payout_freq(
            data.get("payout_freq"), previous.get("payout_freq"), div_confirmed
        )
        dividend_status = data.get("dividend_status") or _resolved_dividend_status(
            dy_to_store, payout_freq, div_confirmed, ticker
        )

        def _positive_or_previous(key: str) -> float:
            value = safe_float(data.get(key))
            return value if value > 0 else safe_float(previous.get(key))

        annual_return_1y = (
            data.get("annual_return_1y")
            if data.get("annual_return_1y") is not None
            else previous.get("annual_return_1y")
        )
        annual_return_3y = (
            data.get("annual_return_3y")
            if data.get("annual_return_3y") is not None
            else previous.get("annual_return_3y")
        )
        annual_return_5y = (
            data.get("annual_return_5y")
            if data.get("annual_return_5y") is not None
            else previous.get("annual_return_5y")
        )
        day_high = safe_float(data.get("day_high")) or cp
        day_low = safe_float(data.get("day_low")) or cp

        cursor.execute("""
            INSERT INTO etf_daily_data
            (ticker, date, current_price, price_change, price_change_percent,
             volume, asset_size, nav, discount_premium,
             dividend_yield, payout_freq,
             dividend_status,
             annual_return_1y, annual_return_3y, annual_return_5y,
             pe_ratio, expense_ratio,
             day_high, day_low, fifty_two_week_high, fifty_two_week_low,
             quote_source, quote_updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,CURRENT_TIMESTAMP)
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
              -- 未確認來源已在 Python 端沿用舊值；此處直接寫入，才能讓
              -- 已確認不配息的商品修正過去誤標的配息頻率。
              payout_freq=VALUES(payout_freq),
              dividend_status=VALUES(dividend_status),
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
              quote_source=IF(VALUES(current_price)>0, VALUES(quote_source), quote_source),
              quote_updated_at=IF(VALUES(current_price)>0, CURRENT_TIMESTAMP, quote_updated_at),
              fifty_two_week_high=VALUES(fifty_two_week_high),
              fifty_two_week_low=VALUES(fifty_two_week_low)
        """, (
            ticker, today, cp,
            safe_float(data.get("price_change")),
            safe_float(data.get("price_change_percent")),
            safe_float(data.get("volume")), _positive_or_previous("asset_size"),
            nav, dp,
            dy_to_store, payout_freq,
            dividend_status,
            annual_return_1y,
            annual_return_3y,
            annual_return_5y,
            _positive_or_previous("pe_ratio"),
            _positive_or_previous("expense_ratio"),
            day_high,
            day_low,
            _positive_or_previous("fifty_two_week_high"),
            _positive_or_previous("fifty_two_week_low"),
            data.get("quote_source") or "unknown",
        ))
        conn.commit()
    cache.delete(f"detail:{ticker}")
    # 行情、規模、殖利率與報酬任一欄位都可能改變五種排行榜。
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
            "quote_date": quote.get("quote_date"),
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
    today  = _quote_date(data.get("quote_date"))
    ticker = data.get("ticker", "")
    cp     = safe_float(data.get("current_price"))

    with get_db() as (conn, cursor):
        cursor.execute("""
            INSERT INTO etf_daily_data
              (ticker, date, current_price, price_change, price_change_percent,
               day_high, day_low, volume, quote_source, quote_updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
            ON DUPLICATE KEY UPDATE
              -- price>0 才覆蓋（save_price_only 同樣需要防零價守衛）
              current_price=IF(VALUES(current_price)>0, VALUES(current_price), current_price),
              price_change=IF(VALUES(current_price)>0, VALUES(price_change), price_change),
              price_change_percent=IF(VALUES(current_price)>0, VALUES(price_change_percent), price_change_percent),
              day_high=IF(VALUES(current_price)>0, VALUES(day_high), day_high),
              day_low=IF(VALUES(current_price)>0, VALUES(day_low), day_low),
              volume=IF(VALUES(current_price)>0, VALUES(volume), volume),
              quote_source=IF(VALUES(current_price)>0, VALUES(quote_source), quote_source),
              quote_updated_at=IF(VALUES(current_price)>0, CURRENT_TIMESTAMP, quote_updated_at)
        """, (
            ticker, today, cp,
            safe_float(data.get("price_change")),
            safe_float(data.get("price_change_percent")),
            safe_float(data.get("day_high", cp)),
            safe_float(data.get("day_low", cp)),
            int(safe_float(data.get("volume", 0))),
            data.get("quote_source") or "unknown",
        ))
        conn.commit()

    cache.delete(f"detail:{ticker}")
    cache.delete_prefix("rank:")


def save_price_bulk(data_list: list) -> int:
    """批次更新報價欄位，一次 DB 事務 + 一次快取清除。

    相比 save_price_only（每檔一次 execute + commit + 多次 cache.delete），此函式：
      · 對「有變化」的資料執行 executemany → 所有列合併為一次 DB 往返（~150ms RTT 降至 1 次）
      · dirty check：price 與 volume 均未變動的 ETF 直接略過，不寫 DB
      · 整批完成後只呼叫 1 次 cache.delete_prefix("rank:")，取代 N 次個別刪除

    回傳值：實際寫入 DB 的列數（dirty check 過濾後）
    """
    if not data_list:
        return 0

    # INSERT 時攜帶上一交易日的補充欄位；同日 UPDATE 仍只更新報價欄位。
    # 這可避免快速行情先建立稀疏新列，導致詳情頁的報酬／殖利率／費用率消失。
    _SQL = """
        INSERT INTO etf_daily_data
          (ticker, date, current_price, price_change, price_change_percent,
           day_high, day_low, volume,
           asset_size, nav, discount_premium,
           dividend_yield, payout_freq,
           dividend_status,
           annual_return_1y, annual_return_3y, annual_return_5y,
           pe_ratio, expense_ratio, fifty_two_week_high, fifty_two_week_low,
           quote_source, quote_updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,CURRENT_TIMESTAMP)
        ON DUPLICATE KEY UPDATE
          current_price         = IF(VALUES(current_price)>0, VALUES(current_price), current_price),
          price_change          = IF(VALUES(current_price)>0, VALUES(price_change), price_change),
          price_change_percent  = IF(VALUES(current_price)>0, VALUES(price_change_percent), price_change_percent),
          day_high              = IF(VALUES(current_price)>0, VALUES(day_high), day_high),
          day_low               = IF(VALUES(current_price)>0, VALUES(day_low), day_low),
          volume                = IF(VALUES(current_price)>0, VALUES(volume), volume),
          quote_source          = IF(VALUES(current_price)>0, VALUES(quote_source), quote_source),
          quote_updated_at      = IF(VALUES(current_price)>0, CURRENT_TIMESTAMP, quote_updated_at)
    """

    rows_to_write: list  = []   # executemany 的參數列表
    dirty_updates: list  = []   # 寫入成功後更新 dirty cache 的 (ticker, price, volume, date)
    tickers_written: set = set()
    source_by_ticker = {
        data.get("ticker", ""): data.get("quote_source") or "unknown"
        for data in data_list
        if data.get("ticker")
    }

    for data in data_list:
        ticker = data.get("ticker", "")
        cp     = safe_float(data.get("current_price"))
        vol    = int(safe_float(data.get("volume", 0)))
        quote_day = _quote_date(data.get("quote_date"))

        if not ticker or cp <= 0:
            continue

        # ── dirty check：price 與 volume 均未改變則跳過 DB 寫入 ──
        # 用 abs 容差 0.001（低於 TW/US ETF 最小 tick），避免浮點誤差誤判
        last = _price_dirty_cache.get(ticker)
        if last is not None:
            last_price, last_vol = last[:2]
            last_day = last[2] if len(last) >= 3 else None
            if (last_day == quote_day and
                    abs(last_price - cp) < 0.001 and last_vol == vol):
                continue   # 未變動，略過 DB 寫入與快取更新

        rows_to_write.append((
            ticker, quote_day, cp,
            safe_float(data.get("price_change")),
            safe_float(data.get("price_change_percent")),
            safe_float(data.get("day_high", cp)),
            safe_float(data.get("day_low",  cp)),
            vol,
        ))
        dirty_updates.append((ticker, cp, vol, quote_day))
        tickers_written.add(ticker)

    if not rows_to_write:
        return 0   # 全部 dirty，無需任何 DB / cache 操作

    # ── 一次讀取各 ticker 上一交易日補充欄位，再批次寫入 ──
    with get_db() as (conn, cursor):
        tickers = [row[0] for row in rows_to_write]
        fmt = ",".join(["%s"] * len(tickers))
        cursor.execute(
            f"""
            SELECT d.asset_size, d.nav, d.discount_premium,
                   d.dividend_yield, d.payout_freq, d.dividend_status,
                   d.annual_return_1y, d.annual_return_3y, d.annual_return_5y,
                   d.pe_ratio, d.expense_ratio,
                   d.fifty_two_week_high, d.fifty_two_week_low,
                   d.ticker
            FROM etf_daily_data d
            INNER JOIN (
                SELECT ticker, MAX(date) AS max_date
                FROM etf_daily_data
                WHERE ticker IN ({fmt})
                GROUP BY ticker
            ) p ON d.ticker=p.ticker AND d.date=p.max_date
            """,
            tickers,
        )
        previous_map = {row["ticker"]: row for row in cursor.fetchall()}

        enriched_rows = []
        for row in rows_to_write:
            previous = previous_map.get(row[0], {})
            enriched_rows.append(row + (
                previous.get("asset_size"),
                previous.get("nav"),
                previous.get("discount_premium"),
                previous.get("dividend_yield"),
                previous.get("payout_freq") or "不配息",
                previous.get("dividend_status") or _resolved_dividend_status(
                    previous.get("dividend_yield"), previous.get("payout_freq"),
                    False, row[0]
                ),
                previous.get("annual_return_1y"),
                previous.get("annual_return_3y"),
                previous.get("annual_return_5y"),
                previous.get("pe_ratio"),
                previous.get("expense_ratio"),
                previous.get("fifty_two_week_high"),
                previous.get("fifty_two_week_low"),
                source_by_ticker.get(row[0], "unknown"),
            ))

        cursor.executemany(_SQL, enriched_rows)
        conn.commit()

    # ── 更新 dirty cache（只在 DB 成功後才更新，保證一致性）──
    for ticker, cp, vol, quote_day in dirty_updates:
        _price_dirty_cache[ticker] = (cp, vol, quote_day)

    # ── 清除 detail 快取（各 ticker 獨立 key）──
    for ticker in tickers_written:
        cache.delete(f"detail:{ticker}")

    # ── 清除排行榜快取（整批只呼叫一次，取代 N × 4 次個別刪除）──
    cache.delete_prefix("rank:")

    return len(rows_to_write)


# ── 需要 pandas ──
try:
    import pandas as pd
except ImportError:
    pd = None
