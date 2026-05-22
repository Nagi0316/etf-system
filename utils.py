"""
utils.py — 共用工具函數
"""
from decimal import Decimal
from datetime import datetime, date
from fastapi.responses import JSONResponse


def convert_value(v):
    if v is None:               return None
    if isinstance(v, Decimal):  return float(v)
    if isinstance(v, datetime): return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):     return v.strftime("%Y-%m-%d")
    return v


def convert_decimal(obj):
    if isinstance(obj, dict):  return {k: convert_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [convert_decimal(i) for i in obj]
    return convert_value(obj)


def safe_json(data, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=convert_decimal(data), status_code=status_code)


def safe_float(v, default: float = 0.0) -> float:
    try:
        f = float(v)
        return f if (f == f) else default  # NaN check
    except Exception:
        return default
