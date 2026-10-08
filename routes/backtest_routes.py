"""
routes/backtest_routes.py — 存股回測 API（含 DRIP、低檔加碼、Benchmark、策略比較）

資料取得架構（與排行榜 / ETF 詳情統一）：
  1. DB     — TW ETF 優先，完全不依賴外部 API
  2. CF Proxy — Cloudflare Worker 代理 Yahoo Finance v8 chart API（繞過 Railway IP 封鎖）
  3. Direct Yahoo — CF Proxy 未設定或失敗時直連（本地開發 / 備援）
"""
import asyncio
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import yfinance as yf
import pandas as pd

from request_models import BacktestIn, BacktestCompareIn
from serialization_utils import safe_json
from services.backtest_engine import run_accumulate, COMMISSION_RATE, MIN_COMMISSION
from services.price_adjustment_service import adjust_detected_splits
from etf_market_data import _cf_yahoo_get, _new_session

logger = logging.getLogger(__name__)
router = APIRouter()
templates: Jinja2Templates | None = None


@router.get("/backtest")
def backtest_page(request: Request):
    return templates.TemplateResponse(request=request, name="backtest_page.html")


def _yahoo_ticker(ticker: str, market: str) -> str:
    """台股 ETF 債券型（00679B 等）在 TWSE 上市，一律用 .TW，不用 .TWO（OTC）。"""
    if market == "TW":
        return f"{ticker}.TW"
    return ticker


def _get_market(ticker: str) -> str:
    from database import get_db
    with get_db() as (conn, cursor):
        cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
        r = cursor.fetchone()
        if r:
            return r["market"]
    return "TW" if ticker[:4].isdigit() else "US"


# ── DB 歷史數據（TW ETF 專用，繞過 Railway IP 封鎖）──────────────────────────

def _download_hist_from_db(ticker: str, start: str, end: str) -> pd.DataFrame:
    """從 etf_daily_data 取得歷史 OHLC，TW ETF 的 yfinance 替代方案。"""
    from database import get_db
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT DATE(date) AS date,
                       MAX(current_price) AS close_price,
                       MAX(COALESCE(NULLIF(day_high, 0), current_price)) AS high_price,
                       MIN(COALESCE(NULLIF(day_low,  0), current_price)) AS low_price
                FROM etf_daily_data
                WHERE ticker = %s AND date >= %s AND date <= %s AND current_price > 0
                GROUP BY DATE(date)
                ORDER BY DATE(date) ASC
            """, (ticker, start, end))
            rows = cursor.fetchall()
        if not rows:
            return pd.DataFrame()
        dates = pd.to_datetime([r["date"] for r in rows])
        closes = [float(r["close_price"]) for r in rows]
        df = pd.DataFrame({
            "Open":  closes,   # DB 無開盤價欄位；以收盤價代入，未宣稱誤差界限
            "Close": closes,
            "High":  [float(r["high_price"])  for r in rows],
            "Low":   [float(r["low_price"])   for r in rows],
        }, index=dates)
        df.index.name = "Date"
        df.attrs["open_proxy"] = True
        logger.info(f"DB hist for {ticker}: {len(df)} rows ({start} → {end})")
        return df
    except Exception as e:
        logger.warning(f"DB hist fetch error for {ticker}: {e}")
        return pd.DataFrame()


def _get_dividends_from_db(ticker: str, start: str, end: str) -> pd.Series:
    """
    取得回測用股息 Series。

    優先策略（依順序）：
    1. etf_dividends 表中的真實歷史配息事件（累積自 Yahoo Finance 抓取）
    缺少事件時回傳空值，不推估歷史股息。
    """
    from database import get_db

    # ── 1. 真實配息事件（最佳）──────────────────────────
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT ex_date, amount FROM etf_dividends
                WHERE ticker = %s AND ex_date >= %s AND ex_date <= %s AND amount > 0
                ORDER BY ex_date ASC
            """, (ticker, start, end))
            rows = cursor.fetchall()

        if rows:
            dates  = pd.to_datetime([str(r["ex_date"])[:10] for r in rows])
            amounts = [float(r["amount"]) for r in rows]
            divs = pd.Series(amounts, index=dates, name="Dividends")
            logger.info(f"Real dividend events for {ticker}: {len(divs)} records ({start}→{end})")
            return divs
    except Exception as e:
        logger.warning(f"etf_dividends query error for {ticker}: {e}")

    # 缺少真實事件時不以今天的殖利率回填歷史，避免前視偏誤。
    return pd.Series(dtype=float)


# ── Yahoo Finance v8 chart API（CF Proxy → 直連 fallback）─────────────────
# 與 etf_routes.py 的 _fetch() 使用完全相同的 CF Proxy + fallback 架構，
# 差異只在於使用 period1/period2 指定日期範圍（而非固定 range 字串）。

def _download_hist_via_cf(symbol: str, start: str, end: str) -> pd.DataFrame:
    """Yahoo Finance v8 chart API，透過 CF Proxy 取得日線 OHLC。
    CF Proxy 未設定時自動降級為直連（本地開發友善）。
    """
    try:
        start_ts = int(datetime.strptime(start, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp())
        end_ts   = int(datetime.strptime(end,   "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp()) + 86400
        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?period1={start_ts}&period2={end_ts}&interval=1d")
        r = (_cf_yahoo_get(url, timeout=20)
             or _new_session(f"https://finance.yahoo.com/quote/{symbol}")
                .get(url, timeout=20))
        if r is None or r.status_code != 200:
            return pd.DataFrame()
        result = r.json().get("chart", {}).get("result")
        if not result:
            return pd.DataFrame()
        ts     = result[0].get("timestamp", [])
        quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = quotes.get("close", [])
        highs  = quotes.get("high",  []) or closes
        lows   = quotes.get("low",   []) or closes
        opens  = quotes.get("open",  []) or closes
        rows = [
            {"date":  datetime.fromtimestamp(t, tz=timezone.utc).date(),
             "Open":  float(o) if o is not None else float(c),
             "Close": float(c),
             "High":  float(h) if h is not None else float(c),
             "Low":   float(l) if l is not None else float(c)}
            for t, o, c, h, l in zip(ts, opens, closes, highs, lows) if c is not None
        ]
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame({
            "Open":  [r["Open"]  for r in rows],
            "Close": [r["Close"] for r in rows],
            "High":  [r["High"]  for r in rows],
            "Low":   [r["Low"]   for r in rows],
        }, index=pd.to_datetime([r["date"] for r in rows]))
        df.index.name = "Date"
        logger.info(f"CF hist {symbol}: {len(df)} rows ({start}→{end})")
        return df
    except Exception as e:
        logger.warning(f"CF hist {symbol}: {e}")
        return pd.DataFrame()


def _get_dividends_via_cf(symbol: str, start: str, end: str) -> pd.Series:
    """Yahoo Finance v8 chart API + events=dividends，透過 CF Proxy 取得配息事件。"""
    try:
        start_ts = int(datetime.strptime(start, "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp())
        end_ts   = int(datetime.strptime(end,   "%Y-%m-%d")
                       .replace(tzinfo=timezone.utc).timestamp()) + 86400
        url = (f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?period1={start_ts}&period2={end_ts}&interval=1d&events=dividends")
        r = (_cf_yahoo_get(url, timeout=15)
             or _new_session(f"https://finance.yahoo.com/quote/{symbol}")
                .get(url, timeout=15))
        if r is None or r.status_code != 200:
            return pd.Series(dtype=float)
        result = r.json().get("chart", {}).get("result")
        if not result:
            return pd.Series(dtype=float)
        events = result[0].get("events", {}).get("dividends", {})
        if not events:
            return pd.Series(dtype=float)
        items = sorted(
            (datetime.fromtimestamp(int(k), tz=timezone.utc).date(),
             float(v["amount"]))
            for k, v in events.items() if v.get("amount")
        )
        if not items:
            return pd.Series(dtype=float)
        divs = pd.Series(
            [amt for _, amt in items],
            index=pd.to_datetime([d for d, _ in items]),
            name="Dividends",
        )
        logger.info(f"CF dividends {symbol}: {len(divs)} events ({start}→{end})")
        return divs
    except Exception as e:
        logger.warning(f"CF dividends {symbol}: {e}")
        return pd.Series(dtype=float)


# ── yfinance 直連（僅作最後 fallback，本地開發或 CF 代理未設定時使用）──────

def _yf_download_safe(symbol: str, start: str, end: str) -> pd.DataFrame:
    """yfinance 單一 symbol 下載，失敗回傳空 DataFrame。"""
    try:
        df = yf.download(symbol, start=start, end=end, progress=False, auto_adjust=False)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        result = pd.DataFrame({
            "Open":  df.get("Open",  df["Close"]).astype(float),
            "Close": df["Close"].astype(float),
            "High":  df.get("High",  df["Close"]).astype(float),
            "Low":   df.get("Low",   df["Close"]).astype(float),
        }, index=df.index)
        if result.index.tz is not None:
            result.index = result.index.tz_localize(None)
        return result
    except Exception as e:
        logger.warning(f"yfinance download failed for {symbol}: {e}")
        return pd.DataFrame()


def _download_hist(yt: str, start: str, end: str,
                   raw_ticker: str = "", market: str = "") -> pd.DataFrame:
    """
    取得歷史 OHLC DataFrame（三層容錯，與排行榜使用同一資料來源）：
      TW ETF：DB → CF Proxy (.TW) → CF Proxy (.TWO) → yfinance (.TW/.TWO)
      US ETF：CF Proxy → yfinance（最後 fallback）
    """
    if market == "TW" and raw_ticker:
        # Layer 1: DB（最快，完全不依賴外部 API）
        df = _download_hist_from_db(raw_ticker, start, end)
        if not df.empty:
            return df
        logger.warning(f"DB hist empty for TW ETF {raw_ticker}, trying CF proxy")
        # Layer 2: CF Proxy，依序試 TWSE (.TW) → TPEX (.TWO)
        for suffix in (".TW", ".TWO"):
            df = _download_hist_via_cf(f"{raw_ticker}{suffix}", start, end)
            if not df.empty:
                return df
        # Layer 3: yfinance 直連（本地開發 / CF 代理未設定）
        for suffix in (".TW", ".TWO"):
            df = _yf_download_safe(f"{raw_ticker}{suffix}", start, end)
            if not df.empty:
                return df
        return pd.DataFrame()

    # US ETF: CF Proxy 優先（繞過 Railway IP 封鎖），yfinance 作最後 fallback
    df = _download_hist_via_cf(yt, start, end)
    if not df.empty:
        return df
    return _yf_download_safe(yt, start, end)


def _get_dividends(yt: str, raw_ticker: str = "", market: str = "",
                   start: str = "", end: str = "") -> pd.Series:
    """
    取得股息 Series（三層容錯）：
      Layer 1: CF Proxy Yahoo v8 events=dividends（US/TW 皆適用，繞過 IP 封鎖）
      Layer 2: yfinance 直連（本地開發 / CF 代理未設定）
      Layer 3: DB 真實配息事件（TW ETF 專用）
    """
    # Layer 1: CF Proxy（先試 yt，TW 再補試 .TWO）
    if start and end:
        symbols_to_try = [yt]
        if market == "TW" and raw_ticker and not yt.endswith(".TWO"):
            symbols_to_try.append(f"{raw_ticker}.TWO")
        for sym in symbols_to_try:
            divs = _get_dividends_via_cf(sym, start, end)
            if not divs.empty:
                return divs

    # Layer 2: yfinance 直連 fallback
    try:
        divs = yf.Ticker(yt).dividends
        if divs is not None and not divs.empty:
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)
            return divs
    except Exception:
        pass

    # Layer 3: TW ETF DB（真實配息事件）
    if market == "TW" and raw_ticker and start and end:
        return _get_dividends_from_db(raw_ticker, start, end)

    return pd.Series(dtype=float)


def _check_data_availability(hist: pd.DataFrame, requested_start: str) -> dict | None:
    """比對實際取得的歷史資料起點與使用者要求的起點。
    若資料明顯較短（差距超過 60 天），回傳警示結構；否則回傳 None。
    """
    if hist.empty:
        return None
    actual_start = hist.index[0]
    req_start    = pd.Timestamp(requested_start)
    if actual_start <= req_start + pd.Timedelta(days=60):
        return None

    actual_years = max(0.0, (hist.index[-1] - actual_start).days / 365.25)
    supported, unsupported = [], []
    for y, label in [(1, "1年"), (3, "3年"), (5, "5年"), (10, "10年")]:
        (supported if actual_years >= y else unsupported).append(label)

    return {
        "has_warning": True,
        "actual_start": actual_start.strftime("%Y/%m"),
        "available_years": round(actual_years, 1),
        "supported_periods": supported,
        "unsupported_periods": unsupported,
        "message": (
            f"⚠ 此 ETF 歷史資料自 {actual_start.strftime('%Y/%m')} 起，"
            f"實際可回測區間已自動調整（{round(actual_years, 1)} 年）"
        ),
    }


def _summary_slim(r: dict) -> dict:
    """回傳摘要欄位（排除 transactions，給策略比較用）"""
    return {k: v for k, v in r.items() if k != "transactions"}


class BacktestDataError(ValueError):
    pass


def _validate_backtest_history(hist: pd.DataFrame):
    if hist.index.has_duplicates or not hist.index.is_monotonic_increasing:
        raise BacktestDataError("歷史價格日期重複或未排序，請先修復資料")
    _, events = adjust_detected_splits(hist.index.strftime("%Y-%m-%d"), hist["Close"])
    if events:
        raise BacktestDataError("歷史價格含疑似分割或反分割斷點；需核實價格、股數與配息基準後才能回測")


def _trading_costs(market: str, body: BacktestIn) -> dict:
    # US 預設採零佣金情境；實際費率可由 API 指定，不套用台股的 20 元低消。
    return {
        "commission_rate": body.commission_rate if body.commission_rate is not None else (COMMISSION_RATE if market == "TW" else 0.0),
        "min_commission": body.min_commission if body.min_commission is not None else (MIN_COMMISSION if market == "TW" else 0.0),
    }


def _exit_tax_rate(ticker: str, market: str, end_date) -> float:
    """台灣 B 類債券 ETF 於法定期間免徵；以實際回測清算日判斷。"""
    if market != "TW":
        return 0.0
    if ticker.upper().endswith("B") and pd.Timestamp("2017-01-01") <= pd.Timestamp(end_date) <= pd.Timestamp("2026-12-31"):
        return 0.0
    return 0.001


# ── API 端點 ─────────────────────────────────────────────────────────────────

@router.post("/api/backtest")
async def run_backtest(body: BacktestIn):
    try:
        market = _get_market(body.ticker)
        yt = _yahoo_ticker(body.ticker, market)

        hist = await asyncio.to_thread(
            _download_hist, yt, body.start_date, body.end_date, body.ticker, market
        )
        if hist.empty:
            return safe_json({
                "status": "error",
                "message": "無法取得歷史數據，請確認代碼與日期範圍（TW ETF 需先在 ETF 詳情頁更新資料）"
            }, 400)

        _validate_backtest_history(hist)
        dividends = await asyncio.to_thread(
            _get_dividends, yt, body.ticker, market, body.start_date, body.end_date
        )

        data_warning = _check_data_availability(hist, body.start_date)

        exit_tax_rate = _exit_tax_rate(body.ticker, market, hist.index[-1])
        result = await asyncio.to_thread(run_accumulate,
            hist,
            initial_amount=body.initial_amount,
            monthly_amount=body.monthly_amount,
            price_mode=body.price_mode,
            enable_drip=body.enable_drip,
            enable_dip=body.enable_dip,
            dip_threshold_20d=body.dip_threshold_20d,
            dip_threshold_60d=body.dip_threshold_60d,
            dip_extra_pct=body.dip_extra_pct,
            dividend_series=dividends,
            exit_tax_rate=exit_tax_rate,
            **_trading_costs(market, body),
        )

        if result.get("error"):
            raise BacktestDataError(result["error"])
        if hist.attrs.get("open_proxy"):
            result["assumptions"].append("資料庫無開盤價，月初買入以首個交易日收盤價代替。")

        # Benchmark 對比
        benchmark_result = None
        if body.benchmark_ticker:
            bm_market = _get_market(body.benchmark_ticker)
            byt = _yahoo_ticker(body.benchmark_ticker, bm_market)
            bhist = await asyncio.to_thread(
                _download_hist, byt, body.start_date, body.end_date, body.benchmark_ticker, bm_market
            )
            if not bhist.empty:
                _validate_backtest_history(bhist)
                bm_exit_tax = _exit_tax_rate(body.benchmark_ticker, bm_market, bhist.index[-1])
                benchmark_result = _summary_slim(
                    await asyncio.to_thread(run_accumulate,
                        bhist, body.initial_amount, body.monthly_amount, body.price_mode,
                        body.enable_drip, False, 10, 15, 50,
                        dividend_series=await asyncio.to_thread(
                            _get_dividends, byt, body.benchmark_ticker, bm_market,
                            body.start_date, body.end_date),
                        exit_tax_rate=bm_exit_tax,
                        **_trading_costs(bm_market, body),
                    )
                )
                benchmark_result["ticker"] = body.benchmark_ticker

        # 策略說明（與純定期定額基準做真實比較，不用靜態估算）
        strategy_note = None
        if body.enable_dip or body.enable_drip:
            enhancements = []
            if body.enable_dip:
                enhancements.append("低檔加碼")
            if body.enable_drip:
                enhancements.append("股息再投入 (DRIP)")
            # 用相同價格模式、期初金額與配息事件比較，不把未再投入的配息丟掉
            baseline = await asyncio.to_thread(run_accumulate,
                hist,
                initial_amount=body.initial_amount,
                monthly_amount=body.monthly_amount,
                price_mode=body.price_mode,
                enable_drip=False,
                enable_dip=False,
                dip_threshold_20d=10,
                dip_threshold_60d=15,
                dip_extra_pct=50,
                dividend_series=dividends,
                exit_tax_rate=exit_tax_rate,
                **_trading_costs(market, body),
            )
            base_ann     = baseline.get("annual_return")
            strategy_ann = result.get("annual_return")
            boost        = round(strategy_ann - base_ann, 2) if base_ann is not None and strategy_ann is not None else None
            strategy_note = {
                "title": f"定期定額 + {'＋'.join(enhancements)} 策略",
                "description": "使用相同價格情境比較策略；加碼與再投入不保證提升報酬。",
                "baseline_annual":  round(base_ann, 2) if base_ann is not None else None,
                "strategy_annual":  round(strategy_ann, 2) if strategy_ann is not None else None,
                "boost":            boost,
                "example": (f"年化差異：{boost:+.1f} 個百分點" if boost is not None
                            else "持有期間不足，無法計算年化差異"),
            }

        return safe_json({
            "status": "success",
            "data": {
                **result,
                "benchmark": benchmark_result,
                "strategy_note": strategy_note,
                "data_warning": data_warning,
                "config": {
                    "ticker": body.ticker,
                    "enable_drip": body.enable_drip,
                    "enable_dip": body.enable_dip,
                }
            }
        })
    except BacktestDataError as ex:
        return safe_json({"status": "error", "message": str(ex)}, 400)
    except Exception as ex:
        logger.error(f"backtest error: {ex}", exc_info=True)
        return safe_json({"status": "error", "message": "回測服務暫時不可用，請稍後重試"}, 500)


@router.post("/api/backtest/compare")
async def compare_strategies(body: BacktestCompareIn):
    """同時跑 4 種策略並回傳比較摘要：月初 / 月最低 / 月最高 / 月初+低檔加碼"""
    try:
        market = _get_market(body.ticker)
        yt = _yahoo_ticker(body.ticker, market)

        hist = await asyncio.to_thread(
            _download_hist, yt, body.start_date, body.end_date, body.ticker, market
        )
        if hist.empty:
            return safe_json({
                "status": "error",
                "message": "無法取得歷史數據，請確認代碼與日期範圍（TW ETF 需先在 ETF 詳情頁更新資料）"
            }, 400)

        _validate_backtest_history(hist)
        dividends = await asyncio.to_thread(
            _get_dividends, yt, body.ticker, market, body.start_date, body.end_date
        )

        cmp_exit_tax = _exit_tax_rate(body.ticker, market, hist.index[-1])
        common = dict(
            hist=hist,
            initial_amount=body.initial_amount,
            monthly_amount=body.monthly_amount,
            enable_drip=body.enable_drip,
            enable_dip=False,
            dip_threshold_20d=body.dip_threshold_20d,
            dip_threshold_60d=body.dip_threshold_60d,
            dip_extra_pct=body.dip_extra_pct,
            dividend_series=dividends,
            exit_tax_rate=cmp_exit_tax,
            **_trading_costs(market, body),
        )

        strategies = {
            "open": await asyncio.to_thread(run_accumulate, **{**common, "price_mode": "open"}),
            "low": await asyncio.to_thread(run_accumulate, **{**common, "price_mode": "low"}),
            "high": await asyncio.to_thread(run_accumulate, **{**common, "price_mode": "high"}),
            "dip": await asyncio.to_thread(run_accumulate, **{**common, "price_mode": "open", "enable_dip": True}),
        }

        if any(v.get("error") for v in strategies.values()):
            raise BacktestDataError("投入金額不足以支付模型手續費")
        return safe_json({
            "status": "success",
            "data": {k: _summary_slim(v) for k, v in strategies.items()},
        })
    except BacktestDataError as ex:
        return safe_json({"status": "error", "message": str(ex)}, 400)
    except Exception as ex:
        logger.error(f"compare error: {ex}", exc_info=True)
        return safe_json({"status": "error", "message": "策略比較服務暫時不可用，請稍後重試"}, 500)
