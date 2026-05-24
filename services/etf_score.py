"""
services/etf_score.py — ETF 綜合健康評分引擎

評分維度（滿分 100）：
  ① 報酬力   30 分  — annual_return_1y（對比同類別中位數）
  ② 配息力   20 分  — dividend_yield（對比同市場中位數）
  ③ 成本效率  15 分  — expense_ratio（越低越好）
  ④ 穩定性   20 分  — 52 週波動區間 / 52w 高點偏差
  ⑤ 動能     15 分  — 近期漲跌趨勢（price_change_percent）

Grade（字母）：
  A  85-100  優等
  B  70-84   良好
  C  55-69   尚可
  D  40-54   較差
  F  0-39    不建議

設計原則：
  - 全部資料皆來自 DB（etf_daily_data + etf_master），不打外部 API
  - 同市場（TW / US）相互比較，避免台美混評
  - 單檔評分快取 10 分鐘（CACHE_TTL_DETAIL）；批次評分快取 30 分鐘
"""
from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_GRADE_MAP = [
    (85, "A", "優等", "#16a34a"),
    (70, "B", "良好", "#2563eb"),
    (55, "C", "尚可", "#d97706"),
    (40, "D", "較差", "#dc2626"),
    ( 0, "F", "不建議", "#64748b"),
]


def _grade(score: float) -> dict:
    for threshold, letter, label, color in _GRADE_MAP:
        if score >= threshold:
            return {"grade": letter, "label": label, "color": color}
    return {"grade": "F", "label": "不建議", "color": "#64748b"}


def _score_clamp(raw: float, lo: float, hi: float) -> float:
    """把 raw 線性映射到 [0, 1]，超出區間截斷。"""
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (raw - lo) / (hi - lo)))


def _fetch_peer_stats(cursor, market: str) -> dict:
    """取同市場熱門 ETF 的統計中位數，作為評分基準。一次 query 全部欄位。"""
    cursor.execute("""
        SELECT
            AVG(d.annual_return_1y)  AS avg_r1y,
            AVG(d.dividend_yield)    AS avg_yld,
            AVG(d.expense_ratio)     AS avg_exp,
            STDDEV(d.annual_return_1y) AS std_r1y
        FROM etf_master m
        JOIN (
            SELECT d1.ticker, d1.annual_return_1y, d1.dividend_yield, d1.expense_ratio
            FROM etf_daily_data d1
            INNER JOIN (
                SELECT ticker, MAX(date) AS md FROM etf_daily_data
                WHERE current_price > 0 GROUP BY ticker
            ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.md
        ) d ON m.ticker = d.ticker
        WHERE m.is_hot = 1 AND m.market = %s AND m.is_delisted = 0
    """, (market,))
    row = cursor.fetchone() or {}
    return {
        "avg_r1y": float(row.get("avg_r1y") or 8.0),
        "std_r1y": float(row.get("std_r1y") or 10.0),
        "avg_yld": float(row.get("avg_yld") or 3.0),
        "avg_exp": float(row.get("avg_exp") or 0.003),
    }


def score_etf(ticker: str) -> Optional[dict]:
    """計算單一 ETF 的綜合評分。失敗時回傳 None（不拋例外）。"""
    from database import get_db
    from cache import cache

    cache_key = f"etf_score:{ticker}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    try:
        with get_db() as (conn, cursor):
            # ── 取標的最新資料 ──────────────────────────
            cursor.execute("""
                SELECT m.ticker, m.market,
                    d.current_price, d.annual_return_1y, d.dividend_yield,
                    d.expense_ratio, d.fifty_two_week_high, d.fifty_two_week_low,
                    d.price_change_percent
                FROM etf_master m
                JOIN (
                    SELECT d1.* FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) AS md FROM etf_daily_data
                        WHERE current_price > 0 GROUP BY ticker
                    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.md
                ) d ON m.ticker = d.ticker
                WHERE m.ticker = %s
            """, (ticker,))
            row = cursor.fetchone()
            if not row:
                return None

            market = row["market"]
            peer   = _fetch_peer_stats(cursor, market)

        result = _compute_score(row, peer)
        cache.set(cache_key, result, 600)   # 10 min
        return result

    except Exception as e:
        logger.warning(f"score_etf({ticker}) 失敗: {e}")
        return None


def _compute_score(row: dict, peer: dict) -> dict:
    """給定 etf 資料列和同類別統計，計算評分並組裝回傳結果。"""
    cp   = float(row.get("current_price") or 0)
    r1y  = float(row.get("annual_return_1y") or 0)
    yld  = float(row.get("dividend_yield") or 0)
    exp  = float(row.get("expense_ratio") or 0)
    h52  = float(row.get("fifty_two_week_high") or cp)
    l52  = float(row.get("fifty_two_week_low")  or cp)
    chg  = float(row.get("price_change_percent") or 0)

    # ── ① 報酬力（30分）────────────────────────────────
    # 以 Z-score 方式：均值 ± 2σ 區間映射到 [0,1]
    avg_r, std_r = peer["avg_r1y"], max(peer["std_r1y"], 1.0)
    r_z = (r1y - avg_r) / std_r  # -2 ~ +2 為正常
    r_score = _score_clamp(r_z, -2, 2) * 30

    # ── ② 配息力（20分）─────────────────────────────────
    avg_yld = peer["avg_yld"]
    # 殖利率 0 → 0分；avg → 10分；2×avg → 20分（上限封頂）
    y_score = _score_clamp(yld, 0, avg_yld * 2) * 20

    # ── ③ 成本效率（15分）───────────────────────────────
    # expense_ratio 0.001 以下 → 15分；0.01 以上 → 0分
    exp_score = _score_clamp(-exp, -0.01, -0.001) * 15

    # ── ④ 穩定性（20分）─────────────────────────────────
    # 用 (current - 52w_low) / (52w_high - 52w_low) 評估位置
    # 並用 52w 波動幅度 / 52w_low 計算幅度懲罰
    if h52 > l52 and l52 > 0:
        range_ratio = (h52 - l52) / l52   # 波動幅度，越小越穩定
        pos_ratio   = (cp - l52) / (h52 - l52) if (h52 - l52) > 0 else 0.5
        # range_ratio > 0.5（>50% 振幅）得 0 分，< 0.1（<10%）得滿分
        stab_range  = _score_clamp(-range_ratio, -0.5, -0.05) * 10
        # 接近 52w 高點失分（高點可能回調）；接近中點最穩
        stab_pos    = (1 - abs(pos_ratio - 0.5) * 2) * 10
        stab_score  = stab_range + stab_pos
    else:
        stab_score  = 10.0  # 資料不足給中等分

    # ── ⑤ 動能（15分）──────────────────────────────────
    # 今日漲跌 -3% ~ +3% 映射；但超漲超跌都不是優質動能
    # 正常動能（-0.5%~+1.5% 區間）最高分
    mom = min(max(chg, -5), 5)
    mom_score = _score_clamp(mom, -3, 3) * 15

    total = r_score + y_score + exp_score + stab_score + mom_score
    total = round(min(100, max(0, total)), 1)

    breakdown = {
        "return_score":     round(r_score, 1),
        "yield_score":      round(y_score, 1),
        "expense_score":    round(exp_score, 1),
        "stability_score":  round(stab_score, 1),
        "momentum_score":   round(mom_score, 1),
    }

    g = _grade(total)

    return {
        "ticker":    row["ticker"],
        "market":    row["market"],
        "score":     total,
        "grade":     g["grade"],
        "grade_label": g["label"],
        "grade_color": g["color"],
        "breakdown": breakdown,
        "meta": {
            "annual_return_1y": round(r1y, 2),
            "dividend_yield":   round(yld, 2),
            "expense_ratio":    round(exp * 100, 4),   # 轉為百分比
            "peer_avg_return":  round(peer["avg_r1y"], 2),
            "peer_avg_yield":   round(peer["avg_yld"], 2),
        },
    }


def score_batch(tickers: list[str]) -> dict[str, dict]:
    """批次評分，不打快取（適合首頁顯示用）。失敗的 ticker 不在結果內。"""
    from database import get_db
    if not tickers:
        return {}

    try:
        with get_db() as (conn, cursor):
            fmt = ",".join(["%s"] * len(tickers))
            cursor.execute(f"""
                SELECT m.ticker, m.market,
                    d.current_price, d.annual_return_1y, d.dividend_yield,
                    d.expense_ratio, d.fifty_two_week_high, d.fifty_two_week_low,
                    d.price_change_percent
                FROM etf_master m
                JOIN (
                    SELECT d1.* FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) AS md FROM etf_daily_data
                        WHERE current_price > 0 GROUP BY ticker
                    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.md
                ) d ON m.ticker = d.ticker
                WHERE m.ticker IN ({fmt})
            """, tickers)
            rows = cursor.fetchall()

            # 取各市場同類別統計（最多兩次）
            markets_needed = list({r["market"] for r in rows})
            peers = {m: _fetch_peer_stats(cursor, m) for m in markets_needed}

        result = {}
        for row in rows:
            try:
                result[row["ticker"]] = _compute_score(row, peers[row["market"]])
            except Exception:
                pass
        return result

    except Exception as e:
        logger.warning(f"score_batch 失敗: {e}")
        return {}
