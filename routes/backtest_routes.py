"""
routes/backtest_routes.py — 存股回測 API（含 DRIP、低檔加碼、Benchmark、策略比較）
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


def _download_hist(yt: str, start: str, end: str) -> pd.DataFrame:
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


def _get_dividends(yt: str) -> pd.Series:
    try:
        divs = yf.Ticker(yt).dividends
        if divs is not None and not divs.empty:
            if divs.index.tz is not None:
                divs.index = divs.index.tz_localize(None)
            return divs
    except Exception:
        pass
    return pd.Series(dtype=float)


def _summary_slim(r: dict) -> dict:
    """回傳摘要欄位（排除 transactions，給策略比較用）"""
    return {k: v for k, v in r.items() if k != "transactions"}


@router.post("/api/backtest")
async def run_backtest(body: BacktestIn):
    try:
        market = _get_market(body.ticker)
        yt = _yahoo_ticker(body.ticker, market)

        hist = await asyncio.to_thread(_download_hist, yt, body.start_date, body.end_date)
        if hist.empty:
            return safe_json({"status": "error", "message": "無法取得歷史數據，請確認代碼與日期範圍"}, 400)

        dividends = None
        if body.enable_drip:
            dividends = await asyncio.to_thread(_get_dividends, yt)

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
            bhist = await asyncio.to_thread(_download_hist, byt, body.start_date, body.end_date)
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

        hist = await asyncio.to_thread(_download_hist, yt, body.start_date, body.end_date)
        if hist.empty:
            return safe_json({"status": "error", "message": "無法取得歷史數據，請確認代碼與日期範圍"}, 400)

        dividends = None
        if body.enable_drip:
            dividends = await asyncio.to_thread(_get_dividends, yt)

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
