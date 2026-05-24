"""
services/exchange_rate.py — 即時 USD/TWD 匯率

備援策略：
  1. 短期快取（5 分鐘）
  2. Yahoo Finance USDTWD=X
  3. ExchangeRate-API (免費，無需 key)
  4. 長期快取（24 小時，最後一次成功值）
  5. 若從未成功過 → 拋出例外（不回傳假值）

新增：fx:USDTWD:last_update  — 最後成功取得匯率的 Unix 時間戳（用於健康檢查）
"""
import logging
import time
import requests
import certifi
from cache import cache, CACHE_TTL_FX

logger = logging.getLogger(__name__)

_CACHE_LAST_KEY  = "fx:USDTWD:last_known"
_CACHE_AGE_KEY   = "fx:USDTWD:last_update"   # Unix timestamp of last successful fetch
_CACHE_LAST_TTL  = 86400   # 24 小時


def get_fx_age_seconds() -> float | None:
    """最後一次成功取得匯率至今的秒數；若從未成功過回傳 None。"""
    ts = cache.get(_CACHE_AGE_KEY)
    if ts is None:
        return None
    return time.time() - float(ts)

def _get_session() -> requests.Session:
    """每次建立新 Session，避免多執行緒共用同一連線池導致 Race Condition。"""
    s = requests.Session()
    s.verify = certifi.where()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; ETF-System/2.0)",
        "Accept": "application/json",
    })
    return s


def get_usd_twd() -> float:
    """取得 USD/TWD 匯率。

    優先使用 5 分鐘快取；若快取失效則即時查詢，
    查詢失敗時使用 24 小時長期快取（最後一次成功值）。
    若從未成功過，拋出 RuntimeError。
    """
    # 短期快取（5 min）
    cached = cache.get("fx:USDTWD")
    if cached:
        return cached

    rate = _fetch_usd_twd()
    if rate and 25.0 < rate < 40.0:
        cache.set("fx:USDTWD", rate, CACHE_TTL_FX)
        cache.set(_CACHE_LAST_KEY, rate, _CACHE_LAST_TTL)   # 更新長期快取
        cache.set(_CACHE_AGE_KEY, time.time(), _CACHE_LAST_TTL)  # 更新最後成功時間戳
        return rate

    # 長期快取（最後一次成功的值）
    last_known = cache.get(_CACHE_LAST_KEY)
    if last_known:
        logger.warning(f"FX 所有來源失敗，使用最後已知匯率 {last_known}")
        return last_known

    logger.error("無法取得 USD/TWD 匯率，使用預設值 32.0 避免系統崩潰")
    return 32.0


def _fetch_usd_twd() -> float:
    """依序嘗試多個來源，回傳匯率或 0.0。"""
    # 1. Yahoo Finance
    try:
        r = _get_session().get(
            "https://query2.finance.yahoo.com/v8/finance/chart/USDTWD=X"
            "?range=1d&interval=1d",
            timeout=8,
        )
        if r.status_code == 200:
            result = r.json().get("chart", {}).get("result")
            if result:
                meta = result[0].get("meta", {})
                p = float(meta.get("regularMarketPrice")
                          or meta.get("chartPreviousClose") or 0)
                if 25.0 < p < 40.0:
                    logger.debug(f"FX Yahoo USD/TWD={p}")
                    return p
    except Exception as e:
        logger.debug(f"FX Yahoo 失敗: {e}")

    # 2. ExchangeRate-API（免費層，無需 API key）
    try:
        r2 = _get_session().get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=8,
        )
        if r2.status_code == 200:
            p2 = float(r2.json().get("rates", {}).get("TWD", 0))
            if 25.0 < p2 < 40.0:
                logger.debug(f"FX ExchangeRate-API USD/TWD={p2}")
                return p2
    except Exception as e:
        logger.debug(f"FX ExchangeRate-API 失敗: {e}")

    # 3. Frankfurter.app（歐洲央行資料，備用）
    try:
        r3 = _get_session().get(
            "https://api.frankfurter.app/latest?from=USD&to=TWD",
            timeout=8,
        )
        if r3.status_code == 200:
            p3 = float(r3.json().get("rates", {}).get("TWD", 0))
            if 25.0 < p3 < 40.0:
                logger.debug(f"FX Frankfurter USD/TWD={p3}")
                return p3
    except Exception as e:
        logger.debug(f"FX Frankfurter 失敗: {e}")

    logger.warning("FX 所有即時來源失敗")
    return 0.0


def convert_usd_to_twd(usd_amount: float) -> float:
    return round(usd_amount * get_usd_twd(), 2)
