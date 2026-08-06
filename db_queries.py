"""Reusable SQL fragments for reading the latest valid ETF quote.

Keeping this join in one place prevents list, watchlist, portfolio, and score
endpoints from drifting to different definitions of "latest".
"""

from typing import Literal


JoinType = Literal["JOIN", "LEFT JOIN"]

LATEST_DAILY_DATA_SUBQUERY = """
    SELECT d1.*
    FROM etf_daily_data d1
    INNER JOIN (
        SELECT ticker, MAX(date) AS max_date
        FROM etf_daily_data
        WHERE current_price > 0
        GROUP BY ticker
    ) d2
      ON d1.ticker = d2.ticker
     AND d1.date = d2.max_date
"""


def latest_daily_join(left_alias: str, join_type: JoinType = "LEFT JOIN") -> str:
    """Build a safe join to each ticker's latest positive-price daily row."""
    if not left_alias.isidentifier():
        raise ValueError(f"Invalid SQL alias: {left_alias!r}")
    if join_type not in ("JOIN", "LEFT JOIN"):
        raise ValueError(f"Unsupported join type: {join_type!r}")

    return f"""
{join_type} (
{LATEST_DAILY_DATA_SUBQUERY}
) d ON {left_alias}.ticker = d.ticker
"""
