"""
models.py — Pydantic 請求 / 回應模型
"""
from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Optional, List
from datetime import date, datetime


# ══════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════

class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6, max_length=128)

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)

class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=6)


# ══════════════════════════════════════════════════════════
#  User
# ══════════════════════════════════════════════════════════

class UpdateProfileIn(BaseModel):
    username: Optional[str] = Field(None, min_length=2, max_length=50)
    phone: Optional[str] = Field(None, max_length=20)
    monthly_budget: Optional[float] = Field(None, ge=0)


# ══════════════════════════════════════════════════════════
#  Watchlist
# ══════════════════════════════════════════════════════════

class WatchlistAddIn(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    name: Optional[str] = None
    market: Optional[str] = None

    @field_validator("ticker")
    @classmethod
    def upper_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: Optional[str]) -> Optional[str]:
        if v and v not in ("TW", "US"):
            raise ValueError("market 必須是 TW 或 US")
        return v


# ══════════════════════════════════════════════════════════
#  Transaction
# ══════════════════════════════════════════════════════════

class TransactionIn(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    transaction_type: str
    shares: float = Field(..., gt=0)
    price: float = Field(..., gt=0)
    commission: float = Field(0.0, ge=0)
    transaction_date: str
    note: Optional[str] = Field(None, max_length=500)
    idempotency_key: Optional[str] = Field(None, max_length=64)  # 前端每次開彈窗產生新 UUID

    @field_validator("transaction_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("buy", "sell"):
            raise ValueError("transaction_type 必須是 buy 或 sell")
        return v

    @field_validator("ticker")
    @classmethod
    def upper_ticker(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("transaction_date")
    @classmethod
    def validate_date_range(cls, v: str) -> str:
        try:
            d = datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError("transaction_date 格式必須為 YYYY-MM-DD")
        today = date.today()
        min_date = date(max(1970, today.year - 50), 1, 1)
        max_date = date(today.year + 1, 12, 31)
        if d < min_date or d > max_date:
            raise ValueError(f"交易日期超出合理範圍（{min_date} ~ {max_date}）")
        return v


# ══════════════════════════════════════════════════════════
#  Backtest
# ══════════════════════════════════════════════════════════

class BacktestIn(BaseModel):
    ticker: str = "0050"
    price_mode: str = "open"  # open | low | high
    start_date: str = "2020-01-01"
    end_date: str = "2024-12-31"
    initial_amount: float = 0.0
    monthly_amount: float = 10000.0
    enable_drip: bool = False        # 股息再投入
    enable_dip: bool = False         # 低檔加碼
    dip_threshold_20d: float = 10.0  # 20日跌幅觸發 (%)
    dip_threshold_60d: float = 15.0  # 60日跌幅觸發 (%)
    dip_extra_pct: float = 50.0      # 加碼比例 (%)
    benchmark_ticker: Optional[str] = None  # 對比 ETF

    @field_validator("ticker", "benchmark_ticker")
    @classmethod
    def upper_ticker(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else v


class BacktestCompareIn(BaseModel):
    ticker: str = "0050"
    start_date: str = "2020-01-01"
    end_date: str = "2024-12-31"
    monthly_amount: float = 10000.0
    initial_amount: float = 0.0
    enable_drip: bool = False
    dip_threshold_20d: float = 10.0
    dip_threshold_60d: float = 15.0
    dip_extra_pct: float = 50.0

    @field_validator("ticker")
    @classmethod
    def upper_ticker(cls, v: str) -> str:
        return v.strip().upper()


# ══════════════════════════════════════════════════════════
#  Price Alert
# ══════════════════════════════════════════════════════════

class PriceAlertIn(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    alert_type: str  # above | below
    target_price: float = Field(..., gt=0)

    @field_validator("alert_type")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in ("above", "below"):
            raise ValueError("alert_type 必須是 above 或 below")
        return v

    @field_validator("ticker")
    @classmethod
    def upper_ticker(cls, v: str) -> str:
        return v.strip().upper()


# ══════════════════════════════════════════════════════════
#  ETF Add to Master
# ══════════════════════════════════════════════════════════

class EtfAddIn(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=20)
    name: str = Field(..., min_length=1, max_length=200)
    market: str

    @field_validator("market")
    @classmethod
    def validate_market(cls, v: str) -> str:
        if v not in ("TW", "US"):
            raise ValueError("market 必須是 TW 或 US")
        return v

    @field_validator("ticker")
    @classmethod
    def upper_ticker(cls, v: str) -> str:
        return v.strip().upper()
