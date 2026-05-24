"""
routes/backtest_routes.py — 存股回測 API（含 DRIP、低檔加碼、Benchmark、策略比較）
TW ETF 歷史數據：優先從 DB (etf_daily_data) 取得，避免 Railway 上 yfinance 被封
"""
import asyncio, logging
from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

import yfinance as yf
import pandas as pd

from models import BacktestIn, BacktestCompareIn
from utils import safe_json
from services.backtest_engine import run_accumulate, run_benchmark

logger = logging.getLogger(__name__)
router = APIRouter()
templates: Jinja2Templates | None = None


@router.get("/backtest")
async def backtest_page(request: Request):
    return templates.TemplateResponse("backtest.html", {"request": request})


def _yahoo_ticker(ticker: str, market: str) -> str:
    if market == "TW":
        return f"{ticker}.TWO" if ticker.upper().endswith("B") else f"{ticker}.TW"
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
        df = pd.DataFrame({
            "Close": [float(r["close_price"]) for r in rows],
            "High":  [float(r["high_price"])  for r in rows],
            "Low":   [float(r["low_price"])   for r in rows],
        }, index=dates)
        df.index.name = "Date"
        logger.info(f"DB hist for {ticker}: {len(df)} rows ({start} → {end})")
        return df
    except Exception as e:
        logger.warning(f"DB hist fetch error for {ticker}: {e}")
        return pd.DataFrame()


def _get_dividends_from_db(ticker: str, start: str, end: str) -> pd.Series:
    """
    從 etf_master 的殖利率＋配息頻率合成股息 Series（TW ETF 用）。
    這是近似值，但比直接失敗好很多。
    """
    from database import get_db
    try:
        with get_db() as (conn, cursor):
            # 從 etf_daily_data 取最新一筆（etf_master 沒有這兩欄）
            cursor.execute("""
                SELECT dividend_yield, payout_freq FROM etf_daily_data
                WHERE ticker=%s AND dividend_yield IS NOT NULL AND dividend_yield > 0
                ORDER BY date DESC LIMIT 1
            """, (ticker,))
            row = cursor.fetchone()
        if not row or not row.get("dividend_yield"):
            return pd.Series(dtype=float)

        annual_yield = float(row["dividend_yield"]) / 100  # DB 存百分比，轉小數
        freq_map = {"月配": 12, "雙月配": 6, "季配": 4, "半年配": 2, "年配": 1}
        payments_per_year = freq_map.get(row.get("payout_freq") or "", 4)

        # 取得歷史價格以計算每次配息金額
        df = _download_hist_from_db(ticker, start, end)
        if df.empty:
            return pd.Series(dtype=float)

        period_str_map = {12: "MS", 6: "2MS", 4: "QS", 2: "6MS", 1: "YS"}
        freq_str = period_str_map.get(payments_per_year, "QS")
        monthly_avg = df["Close"].resample(freq_str).mean()
        dividends = monthly_avg * (annual_yield / payments_per_year)
        dividends = dividends.dropna()
        dividends.name = "Dividends"
        logger.info(f"Synthesized {len(dividends)} dividend events for {ticker} ({payments_per_year}x/year)")
        return dividends
    except Exception as e:
        logger.warning(f"Dividend synthesis error for {ticker}: {e}")
        return pd.Series(dtype=float)


# ── 原始 yfinance 下載（含 DB 優先策略）─────────────────────────────────────

def _download_hist(yt: str, start: str, end: str,
                   raw_ticker: str = "", market: str = "") -> pd.DataFrame:
    """
    取得歷史 OHLC DataFrame。
    TW ETF：先嘗試 DB；DB 空時回退 yfinance（Railway 通常會失敗，但還是試）。
    US ETF：直接用 yfinance。
    """
    if market == "TW" and raw_ticker:
        df = _download_hist_from_db(raw_ticker, start, end)
        if not df.empty:
            return df
        logger.warning(f"DB hist empty for TW ETF {raw_ticker}, falling back to yfinance")

    try:
        df = yf.download(yt, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        result = pd.DataFrame({
            "Close": df["Close"].astype(float),
            "High":  df.get("High", df["Close"]).astype(float),
            "Low":   df.get("Low",  df["Close"]).astype(float),
        }, index=df.index)
        if result.index.tz is not None:
            result.index = result.index.tz_localize(None)
        return result
    except Exception as e:
        logger.warning(f"yfinance download failed for {yt}: {e}")
        return pd.DataFrame()


def _get_dividends(yt: str, raw_ticker: str = "", market: str = "",
                   start: str = "", end: str = "") -> pd.Series:
    """
    取得股息 Series。
    先嘗試 yfinance；失敗時對 TW ETF 合成股息。
    """
    try:
        divs = yf.Ticker(yt).dividends
        if divs is not None and not divs.empty:
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)
            return divs
    except Exception:
        pass

    # TW ETF fallback：從 DB 合成
    if market == "TW" and raw_ticker and start and end:
        return _get_dividends_from_db(raw_ticker, start, end)

    return pd.Series(dtype=float)


def _summary_slim(r: dict) -> dict:
    """回傳摘要欄位（排除 transactions，給策略比較用）"""
    return {k: v for k, v in r.items() if k != "transactions"}


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

        dividends = None
        if body.enable_drip:
            dividends = await asyncio.to_thread(
                _get_dividends, yt, body.ticker, market, body.start_date, body.end_date
            )

        result = run_accumulate(
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
        )

        # Benchmark 對比
        benchmark_result = None
        if body.benchmark_ticker:
            bm_market = _get_market(body.benchmark_ticker)
            byt = _yahoo_ticker(body.benchmark_ticker, bm_market)
            bhist = await asyncio.to_thread(
                _download_hist, byt, body.start_date, body.end_date, body.benchmark_ticker, bm_market
            )
            if not bhist.empty:
                benchmark_result = _summary_slim(
                    run_benchmark(bhist, body.monthly_amount, body.price_mode)
                )
                benchmark_result["ticker"] = body.benchmark_ticker

        # 策略說明
        strategy_note = None
        if body.enable_dip or body.enable_drip:
            enhancements = []
            if body.enable_dip:
                enhancements.append("低檔加碼")
            if body.enable_drip:
                enhancements.append("股息再投入 (DRIP)")
            ann = result.get("annual_return", 0)
            base_ann = ann / 1.3 if ann > 0 else 0
            strategy_note = {
                "title": f"定期定額 + {'＋'.join(enhancements)} 策略",
                "description": "當市場大跌時進行額外加碼，搭配長期持有與股息再投入，可提升長期複利效果。",
                "estimated_boost": "長期年化報酬率可能提升 20%～60%",
                "example": f"純定期定額估算年化：{base_ann:.1f}%  →  搭配策略後：{ann:.1f}%",
            }

        return safe_json({
            "status": "success",
            "data": {
                **result,
                "benchmark": benchmark_result,
                "strategy_note": strategy_note,
                "config": {
                    "ticker": body.ticker,
                    "enable_drip": body.enable_drip,
                    "enable_dip": body.enable_dip,
                }
            }
        })
    except Exception as ex:
        logger.error(f"backtest error: {ex}", exc_info=True)
        return safe_json({"status": "error", "message": f"回測失敗: {ex}"}, 500)


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

        dividends = None
        if body.enable_drip:
            dividends = await asyncio.to_thread(
                _get_dividends, yt, body.ticker, market, body.start_date, body.end_date
            )

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
        )

        strategies = {
            "open": run_accumulate(**{**common, "price_mode": "open"}),
            "low":  run_accumulate(**{**common, "price_mode": "low"}),
            "high": run_accumulate(**{**common, "price_mode": "high"}),
            "dip":  run_accumulate(**{**common, "price_mode": "open", "enable_dip": True}),
        }

        return safe_json({
            "status": "success",
            "data": {k: _summary_slim(v) for k, v in strategies.items()},
        })
    except Exception as ex:
        logger.error(f"compare error: {ex}", exc_info=True)
        return safe_json({"status": "error", "message": f"策略比較失敗: {ex}"}, 500)
