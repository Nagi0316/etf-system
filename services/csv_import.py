"""
services/csv_import.py — CSV 交易記錄匯入
支援欄位：日期, 代碼, 類型(buy/sell), 股數, 價格, 手續費, 備註
"""
import csv, io, logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

logger = logging.getLogger(__name__)

REQUIRED_COLS = {"日期", "代碼", "類型", "股數", "價格"}
ALT_COLS = {
    "日期": ["date", "交易日期", "date"],
    "代碼": ["ticker", "etf", "symbol", "stock"],
    "類型": ["type", "交易類型", "買賣"],
    "股數": ["shares", "數量", "張數"],
    "價格": ["price", "成交價", "成交價格"],
    "手續費": ["commission", "fee", "費用"],
    "備註": ["note", "notes", "memo"],
}

TYPE_MAP = {
    "buy": "buy", "買": "buy", "買入": "buy", "b": "buy",
    "sell": "sell", "賣": "sell", "賣出": "sell", "s": "sell",
}


def parse_csv(content: bytes) -> tuple[list[dict], list[str]]:
    """
    解析 CSV 內容，回傳 (rows, errors)
    rows: 已驗證的交易列表，可直接存入 DB
    errors: 每行的錯誤訊息（空清單表示全部成功）
    """
    rows = []
    errors = []

    try:
        text = content.decode("utf-8-sig")  # 支援 BOM
    except UnicodeDecodeError:
        text = content.decode("big5", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV 檔案無標題行"]

    # 正規化欄位名稱
    col_map = _build_col_map(list(reader.fieldnames))
    missing = REQUIRED_COLS - set(col_map.keys())
    if missing:
        return [], [f"缺少必要欄位：{', '.join(missing)}（或其別名）"]

    for idx, raw_row in enumerate(reader, start=2):
        row_errors = []
        row = {k: raw_row.get(v, "").strip() for k, v in col_map.items()}

        # 日期
        tx_date = _parse_date(row.get("日期", ""))
        if not tx_date:
            row_errors.append(f"第 {idx} 行：日期格式無效 ({row.get('日期')})")

        # 代碼
        ticker = row.get("代碼", "").upper().strip()
        if not ticker:
            row_errors.append(f"第 {idx} 行：代碼為空")

        # 類型
        tx_type_raw = row.get("類型", "").lower().strip()
        tx_type = TYPE_MAP.get(tx_type_raw)
        if not tx_type:
            row_errors.append(f"第 {idx} 行：交易類型無效 ({row.get('類型')})，請使用 buy/sell 或 買/賣")

        # 股數（使用 Decimal 解析，避免浮點精度問題，如 100.00 → 99.9999999）
        try:
            shares = float(Decimal(row.get("股數", "0").replace(",", "")))
            if shares <= 0:
                row_errors.append(f"第 {idx} 行：股數必須大於 0")
        except (InvalidOperation, ValueError):
            row_errors.append(f"第 {idx} 行：股數格式無效 ({row.get('股數')})")
            shares = 0

        # 價格（同上，使用 Decimal 解析）
        try:
            price = float(Decimal(row.get("價格", "0").replace(",", "")))
            if price <= 0:
                row_errors.append(f"第 {idx} 行：價格必須大於 0")
        except (InvalidOperation, ValueError):
            row_errors.append(f"第 {idx} 行：價格格式無效 ({row.get('價格')})")
            price = 0

        # 選填
        try:
            commission = float(Decimal(row.get("手續費", "0").replace(",", "")))
        except (InvalidOperation, ValueError):
            commission = 0.0

        note = row.get("備註", "")

        if row_errors:
            errors.extend(row_errors)
        else:
            rows.append({
                "ticker": ticker,
                "transaction_type": tx_type,
                "shares": shares,
                "price": price,
                "commission": commission,
                "transaction_date": tx_date,
                "note": note[:500] if note else "",
            })

    return rows, errors


def _build_col_map(fieldnames: list[str]) -> dict[str, str]:
    """建立標準欄位名 → CSV 欄位名的映射"""
    lower_fields = {f.lower().strip(): f for f in fieldnames}
    col_map = {}

    for std_col, aliases in ALT_COLS.items():
        # 先嘗試直接匹配
        if std_col in lower_fields:
            col_map[std_col] = lower_fields[std_col]
            continue
        # 嘗試別名
        for alias in aliases:
            if alias.lower() in lower_fields:
                col_map[std_col] = lower_fields[alias.lower()]
                break

    return col_map


def _parse_date(s: str) -> Optional[str]:
    if not s:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y%m%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
