"""
services/backtest_engine.py — 增強版回測引擎
支援：定期定額 | 逢低加碼 | DRIP 股息再投入 | Benchmark 對比

風險指標（run_accumulate 結果內皆包含）：
  - max_drawdown      最大回撤 (%)
  - volatility        年化波動率 (%)
  - sharpe_ratio      夏普比率 (無風險利率 2%)
  - sortino_ratio     索提諾比率（只計下行波動）
  - calmar_ratio      卡瑪比率 = 年化報酬 / |最大回撤|
  - win_rate_monthly  月勝率 (%)
"""
from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.001425 * 0.28  # 28折（台灣券商慣用）
MIN_COMMISSION  = 20.0              # 台股低消 20 元（原值 1.0 遠低於市場行情）
TW_STT_RATE     = 0.001            # 台灣證券交易稅 0.1%（賣出時收取）


def _calc_shares(price: float, budget: float) -> tuple[float, float]:
    """回傳 (shares_bought, fee)"""
    if price <= 0 or budget <= 0:
        return 0.0, 0.0
    fee = max(MIN_COMMISSION, budget * COMMISSION_RATE)
    shares = (budget - fee) / price
    return shares, fee


def run_accumulate(
    hist: pd.DataFrame,
    initial_amount: float,
    monthly_amount: float,
    price_mode: str,
    enable_drip: bool,
    enable_dip: bool,
    dip_threshold_20d: float,
    dip_threshold_60d: float,
    dip_extra_pct: float,
    dividend_series: Optional[pd.Series] = None,
    exit_tax_rate: float = 0.0,
) -> dict:
    """
    定期定額 + 選配低檔加碼 + 選配 DRIP
    """
    from services.alerts import pyramid_extra_amount

    transactions = []
    total_invested = 0.0
    total_shares   = 0.0

    start_dt = hist.index[0]
    end_dt   = hist.index[-1]

    # 初始單筆
    if initial_amount > 0:
        row0 = hist.iloc[:1]
        p = _price_from_mode(row0, price_mode)
        if p > 0:
            shares, fee = _calc_shares(p, initial_amount)
            if shares > 0:
                total_invested += initial_amount
                total_shares   += shares
                transactions.append(_tx("期初單筆", hist.index[0], initial_amount, p, shares, fee, total_shares))

    current_ym = start_dt.to_period("M")
    end_ym     = end_dt.to_period("M")

    while current_ym <= end_ym:
        mask = hist.index.to_period("M") == current_ym
        month_data = hist[mask]
        if month_data.empty:
            current_ym += 1
            continue

        p = _price_from_mode(month_data, price_mode)
        if p <= 0:
            current_ym += 1
            continue

        # ── 低檔加碼 ──
        extra = 0.0
        if enable_dip and len(transactions) > 0:
            # 取最近 65 日的收盤價作為歷史視窗
            window_end_idx = month_data.index[0]
            window = hist[hist.index < window_end_idx]["Close"].tail(65).tolist()
            extra = pyramid_extra_amount(
                monthly_amount, window, p,
                dip_threshold_20d, dip_threshold_60d, dip_extra_pct
            )

        budget = monthly_amount + extra
        shares, fee = _calc_shares(p, budget)
        if shares > 0:
            total_invested += budget
            total_shares   += shares
            tx_type = "低檔加碼" if extra > 0 else "定期定額"
            transactions.append(_tx(tx_type, month_data.index[0], budget, p, shares, fee, total_shares))

        # ── DRIP：配息再投入 ──
        if enable_drip and dividend_series is not None:
            month_divs = dividend_series[dividend_series.index.to_period("M") == current_ym]
            if not month_divs.empty:
                total_div_cash = float(month_divs.sum()) * total_shares
                if total_div_cash > 0:
                    drip_shares, drip_fee = _calc_shares(p, total_div_cash)
                    if drip_shares > 0:
                        total_shares += drip_shares
                        transactions.append(_tx("DRIP配息再投入", month_data.index[-1],
                                               total_div_cash, p, drip_shares, drip_fee, total_shares))

        current_ym += 1

    return _summarize(transactions, total_invested, total_shares, hist, exit_tax_rate)


def run_benchmark(
    hist_benchmark: pd.DataFrame,
    monthly_amount: float,
    price_mode: str,
    exit_tax_rate: float = 0.0,
) -> dict:
    """Benchmark 對比（純定期定額，不含加碼）"""
    return run_accumulate(
        hist_benchmark, 0, monthly_amount, price_mode,
        enable_drip=False, enable_dip=False,
        dip_threshold_20d=10, dip_threshold_60d=15, dip_extra_pct=50,
        exit_tax_rate=exit_tax_rate,
    )


# ── 工具函數 ──

def _price_from_mode(df: pd.DataFrame, mode: str) -> float:
    if df.empty:
        return 0.0
    try:
        if mode == "low":
            return float(df["Low"].min())
        elif mode == "high":
            return float(df["High"].max())
        return float(df["Close"].iloc[0])
    except Exception:
        return 0.0


def _tx(tx_type: str, date, amount: float, price: float, shares: float, fee: float, total_shares: float) -> dict:
    return {
        "date": date.strftime("%Y-%m-%d"),
        "type": tx_type,
        "amount": round(amount, 2),
        "price": round(price, 2),
        "shares_delta": round(shares, 4),
        "total_shares": round(total_shares, 4),
        "market_value": round(total_shares * price, 2),
        "fee": round(fee, 2),
    }


def _trailing_return(hist: pd.DataFrame, years: int) -> float | None:
    """從回測尾端計算純股價 trailing N 年年化報酬率。

    注意：此為純價格報酬（不含 DCA 加碼貢獻與 DRIP 股息再投入），
    用於評估標的本身在該期間的市場表現，與整體策略年化報酬（annual_return）互補。

    允許 ±45 天容差尋找目標日期對應價格；若資料不足（實際跨度 < 預期 70%）則回傳 None。
    """
    if hist.empty:
        return None
    end_date  = hist.index[-1]
    end_price = float(hist["Close"].iloc[-1])
    target    = end_date - pd.DateOffset(years=years)
    # 找距目標日期 ±45 天內最接近的有效收盤
    lo = target - pd.Timedelta(days=45)
    hi = target + pd.Timedelta(days=45)
    window = hist[(hist.index >= lo) & (hist.index <= hi)]
    if window.empty:
        return None
    # 以天數差的絕對值找最近日期（timedelta / 1D 取數值，相容舊版 pandas）
    days_diff    = np.abs((window.index - target) / pd.Timedelta("1D"))
    closest_idx  = int(np.argmin(days_diff))
    ref_date     = window.index[closest_idx]
    ref_price    = float(window["Close"].iloc[closest_idx])
    actual_years = (end_date - ref_date).days / 365.25
    # 實際跨度 < 預期年數的 70% 視為資料不足
    if actual_years < years * 0.7 or ref_price <= 0 or end_price <= 0:
        return None
    return round(((end_price / ref_price) ** (1 / years) - 1) * 100, 2)


def _compute_risk_metrics(hist: pd.DataFrame, annual_return_pct: float) -> dict:
    """從每日收盤價計算六項風險指標，全部皆用真實歷史資料，無推估成分。

    Args:
        hist: OHLC DataFrame，index 為 datetime
        annual_return_pct: 已知年化報酬率（%），用來計算 Calmar

    Returns:
        dict with max_drawdown, volatility, sharpe_ratio,
              sortino_ratio, calmar_ratio, win_rate_monthly
    """
    empty = {
        "max_drawdown": None, "volatility": None,
        "sharpe_ratio": None, "sortino_ratio": None,
        "calmar_ratio": None, "win_rate_monthly": None,
    }
    if hist.empty or len(hist) < 20:
        return empty
    try:
        prices = hist["Close"].dropna()
        if len(prices) < 20:
            return empty

        daily_ret = prices.pct_change().dropna()

        # ── 年化波動率 ──────────────────────────
        vol = float(daily_ret.std() * np.sqrt(252) * 100)

        # ── 最大回撤（Peak-to-Trough）────────────
        cum = (1 + daily_ret).cumprod()
        roll_max = cum.cummax()
        dd_series = (cum - roll_max) / roll_max
        max_dd = float(dd_series.min() * 100)   # 負數，如 -35.2

        # ── Sharpe Ratio（無風險利率 2%）─────────
        RF_ANNUAL = 0.02
        rf_daily  = RF_ANNUAL / 252
        excess    = daily_ret - rf_daily
        sharpe = (float(excess.mean() / excess.std() * np.sqrt(252))
                  if excess.std() > 1e-10 else 0.0)

        # ── Sortino Ratio（僅計下行偏差）─────────
        downside = excess[excess < 0]
        sortino = (float(excess.mean() / downside.std() * np.sqrt(252))
                   if len(downside) > 5 and downside.std() > 1e-10 else 0.0)

        # ── Calmar Ratio = 年化報酬 / |最大回撤| ──
        calmar = abs(annual_return_pct / max_dd) if max_dd < -0.01 else 0.0

        # ── 月勝率 ─────────────────────────────
        monthly = prices.resample("ME").last().pct_change().dropna()
        win_rate = float((monthly > 0).mean() * 100) if len(monthly) >= 3 else None

        return {
            "max_drawdown":     round(max_dd,  2),
            "volatility":       round(vol,     2),
            "sharpe_ratio":     round(sharpe,  2),
            "sortino_ratio":    round(sortino, 2),
            "calmar_ratio":     round(calmar,  2),
            "win_rate_monthly": round(win_rate, 1) if win_rate is not None else None,
        }
    except Exception as e:
        logger.warning(f"_compute_risk_metrics 失敗: {e}")
        return empty


def _summarize(transactions: list, total_invested: float, total_shares: float,
               hist: pd.DataFrame, exit_tax_rate: float = 0.0) -> dict:
    if hist.empty or total_invested <= 0:
        return {"transactions": transactions, "error": "無足夠資料計算摘要"}

    final_price  = float(hist["Close"].iloc[-1])
    gross_value  = total_shares * final_price
    # 台股賣出時需扣除 0.1% 證券交易稅（exit_tax_rate=0.001），US ETF 為 0
    exit_tax     = gross_value * exit_tax_rate
    final_value  = gross_value - exit_tax
    total_profit = final_value - total_invested
    total_return = total_profit / total_invested * 100

    days = (hist.index[-1] - hist.index[0]).days
    years = max(0.1, days / 365.25)
    annual_return = (((final_value / total_invested) ** (1 / years)) - 1) * 100 if final_value > 0 else 0.0

    strategy_boost = None
    if len(transactions) > 0:
        dip_txs = [t for t in transactions if "加碼" in t.get("type", "")]
        if dip_txs:
            strategy_boost = {
                "dip_tx_count": len(dip_txs),
                "extra_invested": round(sum(t["amount"] for t in dip_txs), 2),
            }

    risk = _compute_risk_metrics(hist, annual_return)

    return {
        "total_invested": round(total_invested, 2),
        "final_value": round(final_value, 2),
        "total_profit": round(total_profit, 2),
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2),
        # ── 純股價 trailing 報酬（不含 DCA/DRIP；年數不足則為 None）──
        # 與 annual_return（全期策略報酬）相輔，可評估標的近期市場表現
        "return_3y": _trailing_return(hist, 3),
        "return_5y": _trailing_return(hist, 5),
        "final_price": round(final_price, 2),
        "total_shares": round(total_shares, 4),
        "years_span": round(years, 2),
        "strategy_boost": strategy_boost,
        "transactions": transactions,
        "is_bankrupt": False,
        # ── 風險指標（真實計算，非推估）──
        **risk,
    }
