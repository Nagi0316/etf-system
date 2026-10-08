"""
services/backtest_engine.py — 增強版回測引擎
支援：定期定額 | 逢低加碼 | DRIP 股息再投入 | Benchmark 對比

風險指標（run_accumulate 結果內皆包含）：
  - max_drawdown      最大回撤 (%)
  - volatility        年化波動率 (%)
  - sharpe_ratio      夏普比率 (無風險利率 2%)
  - sortino_ratio     索提諾比率（只計下行波動）
  - calmar_ratio      卡瑪比率 = 標的價格年化報酬 / |價格最大回撤|
  - win_rate_monthly  月勝率 (%)
"""
from __future__ import annotations
import logging
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

COMMISSION_RATE = 0.001425 * 0.28  # 28折（台灣券商慣用）
MIN_COMMISSION  = 20.0              # 台股低消 20 元（原值 1.0 遠低於市場行情）
TW_STT_RATE     = 0.001            # 台灣證券交易稅 0.1%（賣出時收取）


def _calc_shares(price: float, budget: float, commission_rate: float = COMMISSION_RATE,
                 min_commission: float = MIN_COMMISSION) -> tuple[float, float]:
    """回傳 (shares_bought, fee)"""
    if price <= 0 or budget <= 0:
        return 0.0, 0.0
    fee = max(min_commission, budget * commission_rate / (1 + commission_rate))
    if budget <= fee:
        return 0.0, 0.0
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
    commission_rate: float = COMMISSION_RATE,
    min_commission: float = MIN_COMMISSION,
) -> dict:
    """
    定期定額 + 選配低檔加碼 + 選配 DRIP
    """
    from services.price_alert_service import pyramid_extra_amount

    if hist.empty:
        return {"transactions": [], "error": "無足夠資料計算摘要"}
    hist = hist.sort_index()
    transactions = []
    total_invested = total_shares = cash_balance = 0.0

    # 交易依日期執行。月內極值屬事後情境，不能冒充月初可執行策略。
    purchases = {}
    for _, month in hist.groupby(hist.index.to_period("M")):
        if price_mode == "low":
            purchase_date = month["Low"].idxmin()
        elif price_mode == "high":
            purchase_date = month["High"].idxmax()
        else:
            purchase_date = month.index[0]
        purchases[purchase_date] = _price_from_mode(month, price_mode)

    dividends = {}
    if dividend_series is not None:
        for dt, amount in dividend_series.items():
            dt = pd.Timestamp(dt).normalize()
            if hist.index[0] <= dt <= hist.index[-1] and np.isfinite(amount) and amount > 0:
                # 非交易日事件移到下一個有報價日；仍在該日買入前判斷持股。
                pos = hist.index.searchsorted(dt)
                if pos < len(hist):
                    day = hist.index[pos]
                    dividends[day] = dividends.get(day, 0.0) + float(amount)

    for dt, row in hist.iterrows():
        # 除息日才買入的股份不享有該次股息。
        dividend_cash = dividends.get(dt, 0.0) * total_shares
        cash_balance += dividend_cash
        if dividend_cash > 0:
            transactions.append(_tx("現金配息", dt, dividend_cash, float(row["Close"]),
                                    0.0, 0.0, total_shares))

        if dt == hist.index[0] and initial_amount > 0:
            p = _price_from_mode(hist.iloc[:1], price_mode)
            shares, fee = _calc_shares(p, initial_amount, commission_rate, min_commission)
            if shares > 0:
                total_invested += initial_amount
                total_shares += shares
                transactions.append(_tx("期初單筆", dt, initial_amount, p, shares, fee, total_shares))

        if dt in purchases:
            p = purchases[dt]
            extra = 0.0
            if enable_dip and total_shares > 0:
                window = hist.loc[hist.index < dt, "Close"].tail(65).tolist()
                extra = pyramid_extra_amount(monthly_amount, window, p,
                    dip_threshold_20d, dip_threshold_60d, dip_extra_pct)
            budget = monthly_amount + extra
            shares, fee = _calc_shares(p, budget, commission_rate, min_commission)
            if shares > 0:
                total_invested += budget
                total_shares += shares
                tx_type = "低檔加碼" if extra > 0 else "定期定額"
                tx = _tx(tx_type, dt, budget, p, shares, fee, total_shares)
                tx["extra_amount"] = round(extra, 2)
                transactions.append(tx)

        # 缺少實際發放日時，以除息日收盤再投入作近似，明確在 API 揭露。
        if enable_drip and cash_balance > 0:
            p = float(row["Close"])
            shares, fee = _calc_shares(p, cash_balance, commission_rate, min_commission)
            if shares > 0:
                transactions.append(_tx("DRIP配息再投入", dt, cash_balance, p,
                                        shares, fee, total_shares + shares))
                total_shares += shares
                cash_balance = 0.0

    result = _summarize(transactions, total_invested, total_shares, hist,
                        exit_tax_rate, cash_balance)
    result["assumptions"] = [
        "年化報酬使用實際投入日期的資金加權報酬率（XIRR）。",
        "月最低／最高價格為事後情境，不代表可預先執行的交易策略。",
        "風險指標基於標的價格序列，並非定期定額帳戶的報酬序列。",
        "跨市場比較使用標的原幣金額，未模擬歷史換匯；不能當作同台幣投入比較。",
        "允許零碎股；手續費採固定模型，未含個人稅負與期末賣出手續費。",
        "只計入已取得的配息事件；沒有事件不代表該期間沒有配息。",
    ]
    result["trading_costs"] = {"commission_rate": commission_rate, "min_commission": min_commission,
                               "exit_tax_rate": exit_tax_rate}
    result["assumptions"].append(f"模型佣金率 {commission_rate * 100:.4f}%，每筆最低 {min_commission:g} 原幣；不代表所有券商。")
    if enable_drip:
        result["assumptions"].append("DRIP 以除息日收盤價再投入，未模擬發放日與股息稅。")
    return result


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
        # "open": 使用真實開盤價（Yahoo Finance 有 Open 欄位）；
        # DB 來源的 TW ETF 以 Close 代入（DB 無 Open 欄位，已在 backtest_routes 標記）
        if "Open" in df.columns:
            return float(df["Open"].iloc[0])
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


def _compute_risk_metrics(hist: pd.DataFrame, annual_return_pct: float) -> dict:
    """從每日收盤價計算六項風險指標，使用歷史價格與固定年化／無風險利率假設。

    Args:
        hist: OHLC DataFrame，index 為 datetime
        annual_return_pct: 相容舊呼叫介面的參數；Calmar 使用標的價格年化

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
        dd_series = prices / prices.cummax() - 1
        max_dd = float(dd_series.min() * 100)   # 負數，如 -35.2

        # ── Sharpe Ratio（無風險利率 2%）─────────
        RF_ANNUAL = 0.02
        rf_daily  = RF_ANNUAL / 252
        excess    = daily_ret - rf_daily
        sharpe = (float(excess.mean() / excess.std() * np.sqrt(252))
                  if excess.std() > 1e-10 else 0.0)

        # ── Sortino Ratio（僅計下行偏差）─────────
        downside_deviation = float(np.sqrt(np.mean(np.minimum(excess, 0) ** 2)))
        sortino = (float(excess.mean() / downside_deviation * np.sqrt(252))
                   if downside_deviation > 1e-10 else None)

        # ── Calmar Ratio = 年化報酬 / |最大回撤| ──
        years = (prices.index[-1] - prices.index[0]).days / 365.25
        price_annual = ((prices.iloc[-1] / prices.iloc[0]) ** (1 / years) - 1) * 100 if years > 0 else 0
        calmar = float(price_annual / abs(max_dd)) if max_dd < -0.01 else None

        # ── 月勝率 ─────────────────────────────
        monthly = prices.resample("ME").last().pct_change().dropna()
        win_rate = float((monthly > 0).mean() * 100) if len(monthly) >= 3 else None

        return {
            "max_drawdown":     round(max_dd,  2),
            "volatility":       round(vol,     2),
            "sharpe_ratio":     round(sharpe,  2),
            "sortino_ratio":    round(sortino, 2) if sortino is not None else None,
            "calmar_ratio":     round(calmar,  2) if calmar is not None else None,
            "win_rate_monthly": round(win_rate, 1) if win_rate is not None else None,
        }
    except Exception as e:
        logger.warning(f"_compute_risk_metrics 失敗: {e}")
        return empty


def _money_weighted_return(transactions: list, end_date, final_value: float) -> float | None:
    """單向投入與期末資產的 XIRR；同日無持有期間時不虛構年化。"""
    flows = {}
    for tx in transactions:
        if tx["type"] in ("期初單筆", "定期定額", "低檔加碼"):
            dt = pd.Timestamp(tx["date"])
            flows[dt] = flows.get(dt, 0.0) - tx["amount"]
    if not flows or min(flows) >= pd.Timestamp(end_date):
        return None
    if final_value <= 0:
        return -100.0
    end = pd.Timestamp(end_date)
    flows[end] = flows.get(end, 0.0) + final_value
    origin = min(flows)
    amounts = np.array(list(flows.values()))
    years = np.array([(dt - origin).days / 365.25 for dt in flows])
    def npv(log_rate):
        # 共同指數縮放不改變根，避免長期間接近 -100% 時溢位。
        exponents = -log_rate * years
        return float(np.sum(amounts * np.exp(exponents - exponents.max())))
    lo, hi = -16.0, 16.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(160):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return float(np.expm1((lo + hi) / 2) * 100)


def _summarize(transactions: list, total_invested: float, total_shares: float,
               hist: pd.DataFrame, exit_tax_rate: float = 0.0, cash_balance: float = 0.0) -> dict:
    if hist.empty or total_invested <= 0:
        return {"transactions": transactions, "error": "無足夠資料計算摘要"}

    final_price  = float(hist["Close"].iloc[-1])
    gross_value  = total_shares * final_price
    # 台股賣出時需扣除 0.1% 證券交易稅（exit_tax_rate=0.001），US ETF 為 0
    exit_tax     = gross_value * exit_tax_rate
    final_value  = gross_value - exit_tax + cash_balance
    total_profit = final_value - total_invested
    total_return = total_profit / total_invested * 100

    days = (hist.index[-1] - hist.index[0]).days
    years = days / 365.25
    annual_return = _money_weighted_return(transactions, hist.index[-1], final_value)

    strategy_boost = None
    if len(transactions) > 0:
        dip_txs = [t for t in transactions if "加碼" in t.get("type", "")]
        if dip_txs:
            strategy_boost = {
                "dip_tx_count": len(dip_txs),
                "extra_invested": round(sum(t.get("extra_amount", 0) for t in dip_txs), 2),
            }

    risk = _compute_risk_metrics(hist, annual_return)

    return {
        "total_invested": round(total_invested, 2),
        "final_value": round(final_value, 2),
        "total_profit": round(total_profit, 2),
        "total_return": round(total_return, 2),
        "annual_return": round(annual_return, 2) if annual_return is not None else None,
        "annual_return_method": "xirr",
        "cash_balance": round(cash_balance, 2),
        "risk_basis": "underlying_price",
        # 不輸出 return_3y / return_5y：這兩個欄位過去等於整段年化報酬（不是真正的 3Y/5Y 子期間報酬），
        # 容易誤導前端。前端應直接使用 annual_return 搭配 years_span 自行判斷回測長度。
        "final_price": round(final_price, 2),
        "total_shares": round(total_shares, 4),
        "years_span": round(years, 2),
        "strategy_boost": strategy_boost,
        "transactions": transactions,
        "is_bankrupt": False,
        # ── 風險指標（真實計算，非推估）──
        **risk,
    }
