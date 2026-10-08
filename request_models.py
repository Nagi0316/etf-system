"""API 使用的 Pydantic 請求與回應模型。"""
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator, ConfigDict
from typing import Optional, Literal
from datetime import date, datetime


# ══════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════

class RegisterIn(BaseModel):
    username: str = Field(..., min_length=2, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1)

class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


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
    model_config = ConfigDict(allow_inf_nan=False)
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
        if d < min_date or d > today:
            raise ValueError(f"交易日期超出合理範圍（{min_date} ~ {today}）")
        return v


# ══════════════════════════════════════════════════════════
#  Backtest
# ══════════════════════════════════════════════════════════

class BacktestIn(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_period(self):
        try:
            start, end = date.fromisoformat(self.start_date), date.fromisoformat(self.end_date)
        except ValueError:
            raise ValueError("回測日期格式必須為 YYYY-MM-DD")
        if not self.ticker or (self.benchmark_ticker is not None and not self.benchmark_ticker):
            raise ValueError("ETF 代碼不可為空")
        if start > end or end > date.today() or start < date(1970, 1, 1):
            raise ValueError("回測日期區間不合理或包含未來日期")
        if self.initial_amount == 0 and self.monthly_amount == 0:
            raise ValueError("初始或每月投入至少一項必須大於 0")
        return self

    ticker: str = Field("0050", min_length=1, max_length=20)
    price_mode: Literal["open", "low", "high"] = "open"  # open | low | high
    start_date: str = "2020-01-01"
    end_date: str = "2024-12-31"
    initial_amount: float = Field(0.0, ge=0, le=1e12)
    monthly_amount: float = Field(10000.0, ge=0, le=1e12)
    enable_drip: bool = False        # 股息再投入
    enable_dip: bool = False         # 低檔加碼
    dip_threshold_20d: float = Field(10.0, gt=0, le=100)  # 20日跌幅觸發 (%)
    dip_threshold_60d: float = Field(15.0, gt=0, le=100)  # 60日跌幅觸發 (%)
    dip_extra_pct: float = Field(50.0, ge=0, le=1000)      # 加碼比例 (%)
    benchmark_ticker: Optional[str] = Field(None, max_length=20)  # 對比 ETF
    commission_rate: Optional[float] = Field(None, ge=0, le=0.1)
    min_commission: Optional[float] = Field(None, ge=0, le=10000)

    @field_validator("ticker", "benchmark_ticker")
    @classmethod
    def upper_ticker(cls, v: Optional[str]) -> Optional[str]:
        return v.strip().upper() if v else v


class BacktestCompareIn(BacktestIn):
    """策略比較沿用相同輸入驗證，避免比較端點漏驗日期與金額。"""


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
