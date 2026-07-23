"""價格還原工具。

行情供應商有提供 adjusted close 時應優先使用；此模組只作為供應商資料
暫時不可用時的保守備援，僅修正符合常見分割／反分割比例的巨大斷點。
不推測一般漲跌、除息或現金增資，以免把真實市場波動錯當公司行動。
"""
from __future__ import annotations

from typing import Iterable


_COMMON_SPLIT_RATIOS = (
    0.05, 0.1, 0.125, 1 / 7, 0.2, 0.25, 1 / 3, 0.5,
    2.0, 3.0, 4.0, 5.0, 7.0, 10.0, 20.0, 24.0,
)


def _matching_split_ratio(observed_ratio: float, tolerance: float = 0.08) -> float | None:
    """回傳最接近的常見分割比例；一般價格波動回傳 None。"""
    if observed_ratio <= 0 or 0.55 <= observed_ratio <= 1.8:
        return None
    best = min(_COMMON_SPLIT_RATIOS, key=lambda ratio: abs(observed_ratio / ratio - 1))
    return best if abs(observed_ratio / best - 1) <= tolerance else None


def adjust_detected_splits(
    labels: Iterable[str], prices: Iterable[float]
) -> tuple[list[float], list[dict]]:
    """將分割日前的價格換算成分割後基準。

    回傳 ``(adjusted_prices, events)``。事件中的 ratio 定義與證交所一致：
    分割後受益權單位數 / 分割前受益權單位數。
    """
    clean_labels = list(labels)
    adjusted = [float(price) for price in prices]
    events: list[dict] = []

    for index in range(1, min(len(clean_labels), len(adjusted))):
        previous = adjusted[index - 1]
        current = adjusted[index]
        if previous <= 0 or current <= 0:
            continue
        observed = current / previous
        split_ratio = _matching_split_ratio(observed)
        if split_ratio is None:
            continue

        # observed 是「分割後價格 / 分割前價格」，等於 1 / 證交所分割比率。
        twse_ratio = 1 / split_ratio
        for prior in range(index):
            adjusted[prior] *= split_ratio
        events.append({
            "date": clean_labels[index],
            "ratio": round(twse_ratio, 8),
            "observed_price_ratio": round(observed, 8),
        })

    return [round(price, 4) for price in adjusted], events
