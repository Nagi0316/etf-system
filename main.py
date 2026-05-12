"""
ETF 投資管理系統 - main.py
支援 TiDB Cloud (MySQL) 或 本地 SQLite 自動切換
"""
import os, asyncio, random, time, logging, hashlib, secrets, json, shutil
from datetime import datetime, timedelta, date
from decimal import Decimal, ROUND_HALF_UP
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import Optional

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import ssl
ssl._create_default_https_context = ssl._create_unverified_context

from fastapi import FastAPI, Request, HTTPException, Query, File, UploadFile
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import yfinance as yf
import pandas as pd
import numpy as np
import requests as req_lib
from dateutil.relativedelta import relativedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# ─────────────────────────────────────────────
# 日誌
# ─────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
# 目錄設定
# ─────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR    = os.path.join(BASE_DIR, "static")
AVATAR_DIR    = os.path.join(STATIC_DIR, "uploads", "avatars")

for d in [TEMPLATES_DIR, AVATAR_DIR, os.path.join(STATIC_DIR, "css")]:
    os.makedirs(d, exist_ok=True)

# ══════════════════════════════════════════════════════════════════
#  資料庫層 — 支援 TiDB Cloud (MySQL 8 + SSL) 與本地 SQLite 自動切換
#
#  TiDB Cloud 設定方式（三選一）：
#  1. 建立 .env 檔案放在 main.py 同目錄：
#       DB_HOST=gateway01.ap-northeast-1.prod.aws.tidbcloud.com
#       DB_PORT=4000
#       DB_USER=xxxxxxxx.root
#       DB_PASSWORD=xxxxxxxx
#       DB_NAME=etf_tracker
#  2. 在啟動前設定環境變數（Windows CMD）：
#       set DB_HOST=gateway01.ap-northeast-1.prod.aws.tidbcloud.com
#       set DB_PORT=4000
#       set DB_USER=xxxxxxxx.root
#       set DB_PASSWORD=xxxxxxxx
#  3. 直接修改下方 DB_HOST / DB_USER / DB_PASSWORD 常數
# ══════════════════════════════════════════════════════════════════

# ── 載入 .env 檔（有就讀，沒有也不報錯）──
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

DB_HOST     = os.getenv("DB_HOST",     "")
DB_PORT     = int(os.getenv("DB_PORT", "4000"))
DB_USER     = os.getenv("DB_USER",     "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME     = os.getenv("DB_NAME",     "etf_tracker")
SQLITE_PATH = os.path.join(BASE_DIR,   "etf_tracker.db")

USE_MYSQL = bool(DB_HOST and DB_USER and DB_PASSWORD)

# ── TiDB Cloud 連線建立（含 SSL + 自動重試）──
def _get_mysql_conn():
    import mysql.connector
    params = dict(
        host            = DB_HOST,
        port            = DB_PORT,
        user            = DB_USER,
        password        = DB_PASSWORD,
        database        = DB_NAME,
        connection_timeout = 15,
        autocommit      = False,
        charset         = "utf8mb4",
        collation       = "utf8mb4_unicode_ci",
        use_unicode     = True,
    )
    # TiDB Cloud 強制要求 SSL；本地 MySQL 不需要，自動偵測
    if "tidbcloud.com" in DB_HOST or os.getenv("DB_SSL", "").lower() in ("1", "true", "yes"):
        params["ssl_disabled"] = False
        # 若系統有 CA bundle，優先使用；否則略過憑證驗證（僅限內網/測試）
        import certifi, ssl as _ssl
        params["ssl_ca"] = certifi.where()
        params["ssl_verify_cert"] = True
        params["ssl_verify_identity"] = True
    return mysql.connector.connect(**params)

# ── SQLite 連線（本地開發 / 備援）──
def _get_sqlite_conn():
    import sqlite3
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn

# ── 統一 Cursor 包裝（MySQL / SQLite 差異在此處理）──
class DbCursor:
    def __init__(self, raw_cursor, is_mysql: bool):
        self._c         = raw_cursor
        self._is_mysql  = is_mysql
        self.lastrowid  = None

    def execute(self, sql: str, params=()):
        sql_upper = sql.upper().lstrip()
        if self._is_mysql:
            # MySQL/TiDB 不支援 SQLite 的 INSERT OR REPLACE / INSERT OR IGNORE
            if "INSERT OR REPLACE INTO" in sql.upper():
                # → INSERT INTO ... ON DUPLICATE KEY UPDATE 每個欄位都更新
                # 簡單做法：直接換成 REPLACE INTO（MySQL 支援）
                sql = sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO", 1)
            elif "INSERT OR IGNORE INTO" in sql.upper():
                sql = sql.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO", 1)
        else:
            # SQLite：%s → ?，ON DUPLICATE KEY UPDATE → INSERT OR REPLACE
            sql = sql.replace("%s", "?")
            if "ON DUPLICATE KEY UPDATE" in sql.upper():
                sql = self._upsert_to_sqlite(sql)
            elif sql_upper.startswith("INSERT INTO"):
                sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
            elif sql_upper.startswith("REPLACE INTO"):
                sql = sql.replace("REPLACE INTO", "INSERT OR REPLACE INTO", 1)
        self._c.execute(sql, params if params else ())
        self.lastrowid = self._c.lastrowid

    @staticmethod
    def _upsert_to_sqlite(sql: str) -> str:
        idx = sql.upper().index("ON DUPLICATE KEY UPDATE")
        base = sql[:idx].strip().rstrip(",")
        return base.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)

    def fetchone(self):
        row = self._c.fetchone()
        if row is None:
            return None
        return dict(row) if not self._is_mysql else row

    def fetchall(self):
        rows = self._c.fetchall()
        return [dict(r) for r in rows] if not self._is_mysql else rows

    def close(self):
        try: self._c.close()
        except: pass


# ── 連線管理器（context manager）──
_DB_RETRIES = 3

@contextmanager
def get_db(dictionary=True):
    raw_conn  = None
    conn_ctx  = None
    cursor_obj = None
    last_err  = None

    for attempt in range(_DB_RETRIES):
        try:
            if USE_MYSQL:
                raw_conn = _get_mysql_conn()
                raw_cur  = raw_conn.cursor(dictionary=True)
                cursor_obj = DbCursor(raw_cur, is_mysql=True)

                class _Ctx:
                    def commit(self):   raw_conn.commit()
                    def rollback(self): raw_conn.rollback()
                    def close(self):
                        try: raw_conn.close()
                        except: pass
                conn_ctx = _Ctx()
            else:
                raw_conn = _get_sqlite_conn()
                raw_cur  = raw_conn.cursor()
                cursor_obj = DbCursor(raw_cur, is_mysql=False)

                class _Ctx:
                    def commit(self):   raw_conn.commit()
                    def rollback(self): raw_conn.rollback()
                    def close(self):
                        try: raw_conn.close()
                        except: pass
                conn_ctx = _Ctx()
            break   # 連線成功，跳出重試迴圈
        except Exception as e:
            last_err = e
            wait = 1.5 * (2 ** attempt)
            logger.warning(f"DB 連線失敗 (第 {attempt+1}/{_DB_RETRIES} 次): {e}，{wait:.1f}s 後重試")
            time.sleep(wait)

    if conn_ctx is None:
        raise ConnectionError(f"無法連線到資料庫（已重試 {_DB_RETRIES} 次）: {last_err}")

    try:
        yield conn_ctx, cursor_obj
    except Exception:
        try: conn_ctx.rollback()
        except: pass
        raise
    finally:
        try: cursor_obj.close()
        except: pass
        try: conn_ctx.close()
        except: pass


# ─────────────────────────────────────────────
# 初始化資料庫
# ─────────────────────────────────────────────
def init_db():
    """建立資料表（冪等，可重複執行）"""
    if USE_MYSQL:
        # TiDB Cloud：資料庫已在 Dashboard 建好，直接 USE 即可
        # 若是自建 MySQL，嘗試 CREATE DATABASE（已存在不影響）
        import mysql.connector
        try:
            tmp = mysql.connector.connect(
                host=DB_HOST, port=DB_PORT,
                user=DB_USER, password=DB_PASSWORD,
                connection_timeout=10,
                ssl_ca=__import__("certifi").where() if "tidbcloud.com" in DB_HOST else None,
                ssl_verify_cert="tidbcloud.com" in DB_HOST,
                ssl_verify_identity="tidbcloud.com" in DB_HOST,
            )
            tmp.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            tmp.commit(); tmp.close()
        except Exception as e:
            logger.debug(f"CREATE DATABASE 略過（TiDB Cloud 或已存在）: {e}")

    is_sqlite   = not USE_MYSQL
    pk_auto     = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite else "INT AUTO_INCREMENT PRIMARY KEY"
    ts_now      = "DATETIME DEFAULT CURRENT_TIMESTAMP"  if is_sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    ts_update   = "DATETIME DEFAULT CURRENT_TIMESTAMP"  if is_sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"

    ddls = [
        f"""CREATE TABLE IF NOT EXISTS etf_master (
            ticker       VARCHAR(20)  NOT NULL,
            name         VARCHAR(200) NOT NULL,
            market       VARCHAR(20)  NOT NULL,
            category     VARCHAR(100),
            issuer       VARCHAR(100),
            listing_date DATE,
            created_at   {ts_now},
            PRIMARY KEY (ticker)
        ) {'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if not is_sqlite else ''}""",

        f"""CREATE TABLE IF NOT EXISTS etf_daily_data (
            id                   {pk_auto},
            ticker               VARCHAR(20)    NOT NULL,
            date                 DATE           NOT NULL,
            current_price        DECIMAL(10,2)  DEFAULT 0,
            price_change         DECIMAL(10,2)  DEFAULT 0,
            price_change_percent DECIMAL(10,4)  DEFAULT 0,
            volume               BIGINT         DEFAULT 0,
            asset_size           DECIMAL(20,2)  DEFAULT 0,
            nav                  DECIMAL(10,2)  DEFAULT 0,
            dividend_yield       DECIMAL(5,4)   DEFAULT 0,
            payout_freq          VARCHAR(20)    DEFAULT '季配',
            annual_return_1y     DECIMAL(7,4)   DEFAULT 0,
            annual_return_3y     DECIMAL(7,4)   DEFAULT 0,
            annual_return_5y     DECIMAL(7,4)   DEFAULT 0,
            pe_ratio             DECIMAL(10,2)  DEFAULT 0,
            expense_ratio        DECIMAL(6,4)   DEFAULT 0,
            day_high             DECIMAL(10,2)  DEFAULT 0,
            day_low              DECIMAL(10,2)  DEFAULT 0,
            fifty_two_week_high  DECIMAL(10,2)  DEFAULT 0,
            fifty_two_week_low   DECIMAL(10,2)  DEFAULT 0,
            created_at           {ts_now},
            UNIQUE (ticker, date)
        ) {'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if not is_sqlite else ''}""",

        f"""CREATE TABLE IF NOT EXISTS users (
            id            {pk_auto},
            username      VARCHAR(50)  NOT NULL,
            email         VARCHAR(100) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            phone         VARCHAR(20),
            avatar        VARCHAR(255),
            created_at    {ts_now},
            UNIQUE (username),
            UNIQUE (email)
        ) {'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if not is_sqlite else ''}""",

        f"""CREATE TABLE IF NOT EXISTS user_watchlist (
            id        {pk_auto},
            user_id   INT         NOT NULL,
            ticker    VARCHAR(20) NOT NULL,
            added_at  {ts_now},
            UNIQUE (user_id, ticker)
        ) {'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if not is_sqlite else ''}""",

        f"""CREATE TABLE IF NOT EXISTS user_transactions (
            id               {pk_auto},
            user_id          INT            NOT NULL,
            ticker           VARCHAR(20)    NOT NULL,
            transaction_type VARCHAR(10)    NOT NULL,
            shares           DECIMAL(10,4)  NOT NULL,
            price            DECIMAL(10,2)  NOT NULL,
            commission       DECIMAL(10,2)  DEFAULT 0,
            transaction_date DATE           NOT NULL,
            created_at       {ts_now}
        ) {'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if not is_sqlite else ''}""",

        f"""CREATE TABLE IF NOT EXISTS user_portfolio (
            id         {pk_auto},
            user_id    INT            NOT NULL,
            ticker     VARCHAR(20)    NOT NULL,
            shares     DECIMAL(10,4)  NOT NULL DEFAULT 0,
            avg_cost   DECIMAL(10,2)  NOT NULL DEFAULT 0,
            updated_at {ts_update},
            UNIQUE (user_id, ticker)
        ) {'ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci' if not is_sqlite else ''}""",
    ]

    with get_db() as (conn, cursor):
        for ddl in ddls:
            try:
                cursor.execute(ddl)
            except Exception as e:
                logger.debug(f"DDL 略過（已存在或不相容）: {e}")
        conn.commit()

    # ── 自動補欄位（舊資料庫平滑升級）──
    NEW_COLS = [
        ("etf_daily_data", "annual_return_3y",    "DECIMAL(7,4) DEFAULT 0"),
        ("etf_daily_data", "annual_return_5y",    "DECIMAL(7,4) DEFAULT 0"),
        ("etf_daily_data", "fifty_two_week_high", "DECIMAL(10,2) DEFAULT 0"),
        ("etf_daily_data", "fifty_two_week_low",  "DECIMAL(10,2) DEFAULT 0"),
        ("etf_master",     "issuer",              "VARCHAR(100)"),
        ("etf_master",     "listing_date",        "DATE"),
    ]
    with get_db() as (conn, cursor):
        for tbl, col, coldef in NEW_COLS:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {coldef}")
                conn.commit()
                logger.info(f"✅ 新增欄位 {tbl}.{col}")
            except Exception:
                pass  # 欄位已存在，略過

    logger.info(f"✅ 資料庫初始化完成 ({'TiDB/MySQL' if USE_MYSQL else 'SQLite'}：{DB_HOST or SQLITE_PATH})")


# ─────────────────────────────────────────────
# JSON 序列化工具
# ─────────────────────────────────────────────
def convert_value(v):
    if isinstance(v, Decimal):  return float(v)
    if isinstance(v, datetime): return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):     return v.strftime("%Y-%m-%d")
    return v

def convert_decimal(obj):
    if isinstance(obj, dict):  return {k: convert_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):  return [convert_decimal(i) for i in obj]
    return convert_value(obj)

def safe_json(data, status_code=200):
    return JSONResponse(content=convert_decimal(data), status_code=status_code)


# ─────────────────────────────────────────────
# ETF 靜態資料
# ─────────────────────────────────────────────
TW_ETFS = [
    {'ticker':'0050',   'name':'元大台灣50',          'market':'TW'},
    {'ticker':'0056',   'name':'元大高股息',           'market':'TW'},
    {'ticker':'00878',  'name':'國泰永續高股息',        'market':'TW'},
    {'ticker':'006208', 'name':'富邦台50',             'market':'TW'},
    {'ticker':'00919',  'name':'群益台灣精選高息',      'market':'TW'},
    {'ticker':'00929',  'name':'復華台灣科技優息',      'market':'TW'},
    {'ticker':'00713',  'name':'元大台灣高息低波',      'market':'TW'},
    {'ticker':'00940',  'name':'元大台灣價值高息',      'market':'TW'},
    {'ticker':'00939',  'name':'統一台灣高息動能',      'market':'TW'},
    {'ticker':'0052',   'name':'富邦科技',             'market':'TW'},
    {'ticker':'00692',  'name':'富邦公司治理',          'market':'TW'},
    {'ticker':'00679B', 'name':'元大美債20年',          'market':'TW'},
    {'ticker':'00687B', 'name':'國泰20年美債',          'market':'TW'},
    {'ticker':'00751B', 'name':'元大AAA至A公司債',     'market':'TW'},
    {'ticker':'006205', 'name':'富邦上証',             'market':'TW'},
]

US_ETFS = [
    {'ticker':'SPY',  'name':'SPDR S&P 500 ETF Trust',          'market':'US'},
    {'ticker':'QQQ',  'name':'Invesco QQQ Trust',                'market':'US'},
    {'ticker':'VOO',  'name':'Vanguard S&P 500 ETF',             'market':'US'},
    {'ticker':'VTI',  'name':'Vanguard Total Stock Market ETF',  'market':'US'},
    {'ticker':'VT',   'name':'Vanguard Total World Stock ETF',   'market':'US'},
    {'ticker':'SCHD', 'name':'Schwab U.S. Dividend Equity ETF',  'market':'US'},
    {'ticker':'VYM',  'name':'Vanguard High Dividend Yield ETF', 'market':'US'},
    {'ticker':'VIG',  'name':'Vanguard Dividend Appreciation ETF','market':'US'},
    {'ticker':'TLT',  'name':'iShares 20+ Year Treasury Bond ETF','market':'US'},
    {'ticker':'AGG',  'name':'iShares Core U.S. Aggregate Bond ETF','market':'US'},
    {'ticker':'XLK',  'name':'Technology Select Sector SPDR',    'market':'US'},
    {'ticker':'SMH',  'name':'VanEck Semiconductor ETF',         'market':'US'},
    {'ticker':'IWM',  'name':'iShares Russell 2000 ETF',         'market':'US'},
    {'ticker':'GLD',  'name':'SPDR Gold Shares',                 'market':'US'},
    {'ticker':'ARKK', 'name':'ARK Innovation ETF',               'market':'US'},
]

ALL_ETFS = TW_ETFS + US_ETFS

MOCK_DATA = {
    '0050':   (175.50, 1520000, 3050.0e8, 5.2, '季配', 18.5, 1.2),
    '0056':   (38.20,  880000,  1980.0e8, 7.8, '季配', 12.3, 0.8),
    '00878':  (22.15,  1250000, 1560.0e8, 8.2, '季配', 15.6, 1.1),
    '006208': (88.30,  520000,  1250.0e8, 4.5, '季配', 19.2, 1.0),
    '00919':  (21.85,  950000,  890.0e8,  9.5, '月配', 22.8, 1.3),
    '00929':  (18.92,  780000,  720.0e8,  8.8, '月配', 25.3, 1.5),
    '00940':  (15.36,  620000,  580.0e8,  7.2, '月配', 16.8, 0.9),
    '00713':  (52.60,  320000,  680.0e8,  6.5, '季配', 14.2, 0.7),
    '00679B': (34.20,  180000,  350.0e8,  4.8, '月配',  -3.2, -0.5),
    '00692':  (42.80,  210000,  420.0e8,  3.9, '季配', 16.1, 0.9),
    '00939':  (14.55,  480000,  450.0e8,  8.5, '月配', 18.9, 1.1),
    '0051':   (65.20,  95000,   280.0e8,  3.8, '季配', 15.2, 0.8),
    '0052':   (88.50,  75000,   320.0e8,  2.5, '季配', 22.3, 1.4),
    'SPY':    (525.80, 8500000, 5200.0e8, 1.5, '季配', 24.5, 0.8),
    'QQQ':    (445.30, 6200000, 2800.0e8, 0.8, '季配', 32.8, 1.2),
    'VOO':    (482.50, 5200000, 4500.0e8, 1.5, '季配', 24.2, 0.8),
    'VTI':    (258.60, 4800000, 3800.0e8, 1.6, '季配', 22.5, 0.7),
    'VT':     (112.30, 2100000, 1800.0e8, 2.1, '季配', 18.8, 0.6),
    'SCHD':   (78.30,  2100000, 620.0e8,  3.8, '季配', 15.6, 0.5),
    'VYM':    (118.90, 1800000, 580.0e8,  3.2, '季配', 14.8, 0.4),
    'TLT':    (92.40,  3500000, 350.0e8,  4.2, '月配', -5.2, -0.3),
    'AGG':    (98.20,  2800000, 880.0e8,  3.8, '月配',  1.2, 0.1),
    'XLK':    (218.50, 3200000, 720.0e8,  0.6, '季配', 38.2, 1.8),
    'IWM':    (198.30, 4100000, 610.0e8,  1.2, '季配', 12.5, 0.7),
    'ARKK':   (52.80,  3800000, 80.0e8,   0.0, '無配息', -8.5, -1.2),
    'SMH':    (238.50, 2100000, 280.0e8,  0.5, '季配', 45.8, 2.1),
    'DIA':    (395.60, 1800000, 350.0e8,  1.8, '月配', 12.8, 0.5),
}

def _rand_price(base):
    return round(base * random.uniform(0.98, 1.02), 2)

def get_mock_row(ticker):
    d = MOCK_DATA.get(ticker)
    if not d:
        return None
    price, vol, assets, yld, freq, ret, chg = d
    price = _rand_price(price)
    chg_pct = chg + random.uniform(-0.3, 0.3)
    return {
        'ticker': ticker,
        'current_price': price,
        'price_change': round(price * chg_pct / 100, 2),
        'price_change_percent': round(chg_pct, 2),
        'volume': vol,
        'asset_size': assets,
        'nav': price,
        'dividend_yield': yld,
        'payout_freq': freq,
        'annual_return_1y': ret,
        'pe_ratio': 0.0,
        'expense_ratio': 0.0,
        'day_high': round(price * 1.005, 2),
        'day_low': round(price * 0.995, 2),
        'followers': 0,
    }


def insert_mock_data():
    """只確保 etf_master 有基礎清單，不塞假的每日數據"""
    with get_db() as (conn, cursor):
        for etf in ALL_ETFS:
            cursor.execute(
                """INSERT INTO etf_master (ticker, name, market)
                   VALUES (%s, %s, %s)
                   ON DUPLICATE KEY UPDATE name=VALUES(name), market=VALUES(market)""",
                (etf['ticker'], etf['name'], etf['market'])
            )
        conn.commit()
    logger.info("✅ etf_master 基礎清單已確認")


# ══════════════════════════════════════════════════════════════════
#  多源數據抓取層
#  優先順序：
#    台股 → 台灣證交所 API (TWSE/MIS)  → yfinance download() 備援
#    美股 → Yahoo Finance Query2 API   → yfinance download() 備援
# ══════════════════════════════════════════════════════════════════

def _yahoo_ticker(ticker: str, market: str) -> str:
    if market == 'TW':
        return f"{ticker}.TWO" if ticker.upper().endswith('B') else f"{ticker}.TW"
    return ticker

def _safe_float(v, default=0.0) -> float:
    try:
        f = float(v)
        return f if (f == f) else default
    except Exception:
        return default

def _annualized_return(closes: list, years: float) -> float:
    """closes: list of float，時間序列"""
    if not closes or len(closes) < 5:
        return 0.0
    try:
        p0, p1 = float(closes[0]), float(closes[-1])
        if p0 <= 0:
            return 0.0
        total = (p1 - p0) / p0
        if years < 1:
            return round(total * 100, 2)
        ann = ((1 + total) ** (1 / years)) - 1
        return round(ann * 100, 2)
    except Exception:
        return 0.0

# ── UA 池，隨機輪換 ──
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

def _new_session() -> req_lib.Session:
    s = req_lib.Session()
    s.headers.update({
        "User-Agent": random.choice(_UA_POOL),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
    })
    return s

_shared_session: Optional[req_lib.Session] = None
def _get_yf_session():
    global _shared_session
    if _shared_session is None:
        _shared_session = _new_session()
    return _shared_session


# ────────────────────────────────────────────
# 台股：直接打台灣證交所 / 台灣期交所 API
# ────────────────────────────────────────────
def _fetch_tw_realtime(ticker: str) -> Optional[dict]:
    """
    台灣證交所 MIS 即時報價 API
    上市 (TWSE): https://mis.twse.com.tw/stock/api/getStockInfo.jsp
    上櫃 (OTC): https://mis.tpex.org.tw/
    """
    is_otc = ticker.upper().endswith('B') or ticker.startswith('006')
    # 統一試 TWSE 格式
    stock_id = f"tse_{ticker}.tw"
    url = (
        f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        f"?ex_ch={stock_id}&json=1&delay=0"
    )
    try:
        s = _new_session()
        s.headers["Referer"] = "https://mis.twse.com.tw/"
        r = s.get(url, timeout=8)
        data = r.json()
        items = data.get("msgArray", [])
        if not items:
            # 試上櫃
            stock_id2 = f"otc_{ticker}.tw"
            url2 = url.replace(stock_id, stock_id2)
            r2 = s.get(url2, timeout=8)
            data2 = r2.json()
            items = data2.get("msgArray", [])
        if not items:
            return None
        d = items[0]
        # z=成交價, y=昨收, h=最高, l=最低, v=成交量(千股), n=名稱
        price   = _safe_float(d.get("z") or d.get("b"))   # z=成交 b=買一
        prev    = _safe_float(d.get("y"))
        high    = _safe_float(d.get("h"))
        low     = _safe_float(d.get("l"))
        vol_k   = _safe_float(d.get("v"))                  # 千股
        name    = d.get("n", ticker)
        if price <= 0:
            return None
        chg     = round(price - prev, 4)
        chg_pct = round(chg / prev * 100, 4) if prev > 0 else 0.0
        return {
            "current_price":        price,
            "price_change":         chg,
            "price_change_percent": chg_pct,
            "day_high":             high,
            "day_low":              low,
            "volume":               int(vol_k * 1000),
            "name_tw":              name,
        }
    except Exception as e:
        logger.debug(f"TWSE MIS {ticker}: {e}")
        return None


def _fetch_tw_history_twse(ticker: str) -> list:
    """
    台灣證交所月均價歷史 API（最近 60 個月）
    回傳 [(date_str, close_price), ...]
    """
    results = []
    now = datetime.now()
    for delta in range(0, 61, 3):  # 每次查 3 個月，共 20 次
        dt = now - relativedelta(months=delta)
        ym = dt.strftime("%Y%m01")
        url = (
            f"https://www.twse.com.tw/exchangeReport/STOCK_DAY"
            f"?response=json&date={ym}&stockNo={ticker}"
        )
        try:
            s = _new_session()
            r = s.get(url, timeout=8)
            d = r.json()
            for row in d.get("data", []):
                # row: [日期, 成交股數, 成交金額, 開盤, 最高, 最低, 收盤, 漲跌, 成交筆數]
                try:
                    close = _safe_float(row[6].replace(",", ""))
                    if close > 0:
                        results.append(close)
                except Exception:
                    pass
        except Exception:
            pass
    results.reverse()
    return results


def _fetch_tw_dividend_twse(ticker: str, current_price: float) -> tuple:
    """用 TWSE 配息公告算殖利率"""
    try:
        url = f"https://www.twse.com.tw/exchangeReport/TWT48U?response=json&stockNo={ticker}"
        s = _new_session()
        r = s.get(url, timeout=8)
        d = r.json()
        rows = d.get("data", [])
        if not rows:
            return 0.0, "不配息"
        # 只取近一年的記錄（最新 rows）
        year_now = datetime.now().year
        year_rows = [row for row in rows if len(row) > 0 and str(year_now - 1) in str(row[0])]
        if not year_rows:
            year_rows = rows[-4:] if len(rows) >= 4 else rows
        # 每筆: [..., 現金股利, ...]  欄位索引依格式而定
        total_div = 0.0
        for row in year_rows:
            for cell in row:
                try:
                    v = _safe_float(str(cell).replace(",", ""))
                    if 0 < v < 20:  # 合理範圍的股息數字
                        total_div += v
                        break
                except Exception:
                    pass
        n = len(year_rows)
        div_yield = round(total_div / current_price * 100, 4) if current_price > 0 else 0.0
        if   n >= 10: freq = "月配"
        elif n >= 3:  freq = "季配"
        elif n == 2:  freq = "半年配"
        elif n == 1:  freq = "年配"
        else:         freq = "不配息"
        return div_yield, freq
    except Exception:
        return 0.0, "不配息"


# ────────────────────────────────────────────
# 美股：Yahoo Finance Query2 REST API（繞過 yfinance）
# ────────────────────────────────────────────
def _fetch_us_quote_query2(ticker: str) -> Optional[dict]:
    """
    直接打 Yahoo Finance v8 chart API，不透過 yfinance library，
    避免 yfinance 自己的限流層。
    """
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range=10d&interval=1d&includePrePost=false"
    )
    try:
        s = _new_session()
        s.headers["Origin"]  = "https://finance.yahoo.com"
        s.headers["Referer"] = f"https://finance.yahoo.com/quote/{ticker}"
        r = s.get(url, timeout=10)
        if r.status_code == 429:
            logger.warning(f"Query2 {ticker}: 429，等 15 秒後重試")
            time.sleep(15)
            r = _new_session().get(url, timeout=10)
        if r.status_code != 200:
            return None
        j = r.json()
        result = j.get("chart", {}).get("result")
        if not result:
            return None
        meta    = result[0].get("meta", {})
        quotes  = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes  = [c for c in (quotes.get("close") or []) if c is not None]
        highs   = [h for h in (quotes.get("high")  or []) if h is not None]
        lows    = [l for l in (quotes.get("low")   or []) if l is not None]
        volumes = [v for v in (quotes.get("volume") or []) if v is not None]

        if len(closes) < 2:
            return None

        price   = _safe_float(closes[-1])
        prev    = _safe_float(closes[-2])
        chg     = round(price - prev, 4)
        chg_pct = round(chg / prev * 100, 4) if prev > 0 else 0.0

        return {
            "current_price":        price,
            "price_change":         chg,
            "price_change_percent": chg_pct,
            "day_high":             _safe_float(highs[-1])   if highs   else price,
            "day_low":              _safe_float(lows[-1])    if lows    else price,
            "volume":               int(volumes[-1])          if volumes else 0,
            "prev_close":           prev,
        }
    except Exception as e:
        logger.debug(f"Query2 quote {ticker}: {e}")
        return None


def _fetch_us_history_query2(ticker: str, years: int = 5) -> list:
    """
    打 Yahoo Finance v8 chart API 抓 5 年月線，回傳 close list
    """
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range={years}y&interval=1mo&includePrePost=false"
    )
    try:
        s = _new_session()
        s.headers["Referer"] = f"https://finance.yahoo.com/quote/{ticker}"
        r = s.get(url, timeout=12)
        if r.status_code != 200:
            return []
        j = r.json()
        result = j.get("chart", {}).get("result")
        if not result:
            return []
        quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
        closes = [_safe_float(c) for c in (quotes.get("close") or []) if c is not None]
        return closes
    except Exception as e:
        logger.debug(f"Query2 history {ticker}: {e}")
        return []


def _fetch_us_detail_query1(ticker: str) -> dict:
    """
    打 Yahoo Finance quoteSummary API 抓詳細資料
    (expense_ratio, PE, AUM, fundFamily, inception 等)
    """
    url = (
        f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}"
        f"?modules=summaryDetail,defaultKeyStatistics,fundProfile,topHoldings"
    )
    try:
        s = _new_session()
        s.headers["Referer"] = f"https://finance.yahoo.com/quote/{ticker}"
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return {}
        j = r.json()
        qs = j.get("quoteSummary", {}).get("result", [{}])
        if not qs:
            return {}
        merged = {}
        for section in qs[0].values():
            if isinstance(section, dict):
                for k, v in section.items():
                    if isinstance(v, dict) and "raw" in v:
                        merged[k] = v["raw"]
                    elif not isinstance(v, (dict, list)):
                        merged[k] = v
        return merged
    except Exception as e:
        logger.debug(f"quoteSummary {ticker}: {e}")
        return {}


def _fetch_us_dividends_query2(ticker: str, current_price: float) -> tuple:
    """抓近一年配息記錄，計算殖利率和頻率"""
    url = (
        f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?range=2y&interval=1mo&events=dividends&includePrePost=false"
    )
    try:
        s = _new_session()
        s.headers["Referer"] = f"https://finance.yahoo.com/quote/{ticker}"
        r = s.get(url, timeout=10)
        if r.status_code != 200:
            return 0.0, "不配息"
        j = r.json()
        result = j.get("chart", {}).get("result")
        if not result:
            return 0.0, "不配息"
        events = result[0].get("events", {}).get("dividends", {})
        if not events:
            return 0.0, "不配息"
        cutoff = time.time() - 365 * 86400
        recent = [v["amount"] for v in events.values()
                  if v.get("date", 0) >= cutoff and v.get("amount", 0) > 0]
        if not recent:
            return 0.0, "不配息"
        total = sum(recent)
        div_yield = round(total / current_price * 100, 4) if current_price > 0 else 0.0
        n = len(recent)
        if   n >= 10: freq = "月配"
        elif n >= 3:  freq = "季配"
        elif n == 2:  freq = "半年配"
        elif n == 1:  freq = "年配"
        else:         freq = "不配息"
        return div_yield, freq
    except Exception as e:
        logger.debug(f"dividends {ticker}: {e}")
        return 0.0, "不配息"


# ────────────────────────────────────────────
# 主抓取函數：台股 / 美股 分流
# ────────────────────────────────────────────
def fetch_one_etf(ticker: str, market: str) -> Optional[dict]:
    if market == 'TW':
        return _fetch_tw_etf(ticker)
    else:
        return _fetch_us_etf(ticker)


def _fetch_tw_etf(ticker: str) -> Optional[dict]:
    """台股 ETF：TWSE MIS 即時 + TWSE 歷史"""
    quote = _fetch_tw_realtime(ticker)
    if not quote:
        # TWSE 即時失敗 → 試 yfinance download（不同 endpoint）
        try:
            yt = _yahoo_ticker(ticker, 'TW')
            df = yf.download(yt, period="10d", interval="1d",
                             progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 2:
                price = float(df['Close'].iloc[-1])
                prev  = float(df['Close'].iloc[-2])
                chg   = round(price - prev, 4)
                quote = {
                    "current_price": price,
                    "price_change": chg,
                    "price_change_percent": round(chg/prev*100, 4) if prev > 0 else 0.0,
                    "day_high": float(df['High'].iloc[-1]),
                    "day_low":  float(df['Low'].iloc[-1]),
                    "volume":   int(df['Volume'].iloc[-1]),
                }
        except Exception as e:
            logger.debug(f"yf.download TW {ticker}: {e}")

    if not quote:
        return None

    price = quote["current_price"]

    # 歷史報酬（TWSE 月均）
    history_closes = _fetch_tw_history_twse(ticker)
    now_ts = datetime.now()
    cutoff_1y = len(history_closes) - 12 if len(history_closes) >= 12 else 0
    cutoff_3y = len(history_closes) - 36 if len(history_closes) >= 36 else 0
    annual_return_1y = _annualized_return(history_closes[cutoff_1y:], 1.0)
    annual_return_3y = _annualized_return(history_closes[cutoff_3y:], 3.0)
    annual_return_5y = _annualized_return(history_closes, 5.0)

    # 52週高低（從月均歷史取近 12 個月）
    last12 = history_closes[-12:] if len(history_closes) >= 12 else history_closes
    wk52_high = max(last12) if last12 else quote["day_high"]
    wk52_low  = min(last12) if last12 else quote["day_low"]

    # 殖利率
    div_yield, payout_freq = _fetch_tw_dividend_twse(ticker, price)

    # ETF master 的靜態資料（名稱已在 etf_master，不需再抓）
    logger.info(
        f"✅ {ticker}[TW]: {price} ({quote['price_change_percent']:+.2f}%) "
        f"量={quote['volume']:,} 息={div_yield:.2f}%/{payout_freq} "
        f"1y={annual_return_1y:+.1f}%"
    )
    return {
        'ticker':               ticker,
        'current_price':        price,
        'price_change':         quote["price_change"],
        'price_change_percent': quote["price_change_percent"],
        'day_high':             quote["day_high"],
        'day_low':              quote["day_low"],
        'fifty_two_week_high':  wk52_high,
        'fifty_two_week_low':   wk52_low,
        'volume':               quote["volume"],
        'asset_size':           0.0,
        'nav':                  price,
        'pe_ratio':             0.0,
        'expense_ratio':        0.0,
        'dividend_yield':       div_yield,
        'payout_freq':          payout_freq,
        'annual_return_1y':     annual_return_1y,
        'annual_return_3y':     annual_return_3y,
        'annual_return_5y':     annual_return_5y,
    }


def _fetch_us_etf(ticker: str) -> Optional[dict]:
    """美股 ETF：Yahoo Query2 REST API（繞過 yfinance）"""
    quote = _fetch_us_quote_query2(ticker)
    if not quote:
        # 備援：yfinance download
        try:
            df = yf.download(ticker, period="10d", interval="1d",
                             progress=False, auto_adjust=True)
            if not df.empty and len(df) >= 2:
                price = float(df['Close'].iloc[-1])
                prev  = float(df['Close'].iloc[-2])
                chg   = round(price - prev, 4)
                quote = {
                    "current_price": price,
                    "price_change": chg,
                    "price_change_percent": round(chg/prev*100, 4) if prev > 0 else 0.0,
                    "day_high": float(df['High'].iloc[-1]),
                    "day_low":  float(df['Low'].iloc[-1]),
                    "volume":   int(df['Volume'].iloc[-1]),
                }
        except Exception as e:
            logger.debug(f"yf.download US {ticker}: {e}")

    if not quote:
        return None

    price = quote["current_price"]

    # 歷史報酬
    history = _fetch_us_history_query2(ticker, years=5)
    cutoff_1y = len(history) - 12 if len(history) >= 12 else 0
    cutoff_3y = len(history) - 36 if len(history) >= 36 else 0
    annual_return_1y = _annualized_return(history[cutoff_1y:], 1.0)
    annual_return_3y = _annualized_return(history[cutoff_3y:], 3.0)
    annual_return_5y = _annualized_return(history, 5.0)

    # 52週高低
    last12 = history[-12:] if len(history) >= 12 else history
    wk52_high = max(last12) if last12 else quote["day_high"]
    wk52_low  = min(last12) if last12 else quote["day_low"]

    # 殖利率
    div_yield, payout_freq = _fetch_us_dividends_query2(ticker, price)

    # 詳細資料 (AUM, PE, expense_ratio, issuer, inception)
    detail    = _fetch_us_detail_query1(ticker)
    asset_size    = _safe_float(detail.get('totalAssets') or detail.get('netAssets'))
    pe_ratio      = _safe_float(detail.get('trailingPE') or detail.get('forwardPE'))
    expense_ratio = _safe_float(detail.get('annualReportExpenseRatio') or detail.get('expenseRatio'))
    issuer        = (detail.get('fundFamily') or '')[:100]
    nav           = _safe_float(detail.get('navPrice') or price)

    # yfinance 殖利率補充
    yf_yield = _safe_float(detail.get('yield') or detail.get('dividendYield')) * 100
    if yf_yield > div_yield:
        div_yield = round(yf_yield, 4)
        if div_yield > 0 and payout_freq == '不配息':
            payout_freq = '季配'

    # 寫 issuer / listing_date 到 etf_master
    listing_date = None
    raw_ld = detail.get('fundInceptionDate')
    if raw_ld:
        try:
            listing_date = datetime.fromtimestamp(int(raw_ld)).strftime('%Y-%m-%d')
        except Exception:
            pass
    if issuer or listing_date:
        try:
            with get_db() as (conn, cursor):
                if issuer and listing_date:
                    cursor.execute("UPDATE etf_master SET issuer=%s, listing_date=%s WHERE ticker=%s",
                                   (issuer, listing_date, ticker))
                elif issuer:
                    cursor.execute("UPDATE etf_master SET issuer=%s WHERE ticker=%s", (issuer, ticker))
                elif listing_date:
                    cursor.execute("UPDATE etf_master SET listing_date=%s WHERE ticker=%s", (listing_date, ticker))
                conn.commit()
        except Exception:
            pass

    logger.info(
        f"✅ {ticker}[US]: {price} ({quote['price_change_percent']:+.2f}%) "
        f"量={quote['volume']:,} 息={div_yield:.2f}%/{payout_freq} "
        f"1y={annual_return_1y:+.1f}% AUM={asset_size/1e9:.1f}B"
    )
    return {
        'ticker':               ticker,
        'current_price':        price,
        'price_change':         quote["price_change"],
        'price_change_percent': quote["price_change_percent"],
        'day_high':             quote["day_high"],
        'day_low':              quote["day_low"],
        'fifty_two_week_high':  wk52_high,
        'fifty_two_week_low':   wk52_low,
        'volume':               quote["volume"],
        'asset_size':           asset_size,
        'nav':                  nav,
        'pe_ratio':             pe_ratio,
        'expense_ratio':        expense_ratio,
        'dividend_yield':       div_yield,
        'payout_freq':          payout_freq,
        'annual_return_1y':     annual_return_1y,
        'annual_return_3y':     annual_return_3y,
        'annual_return_5y':     annual_return_5y,
    }



async def update_all_etf_data():
    today = datetime.now().date()
    updated = 0
    failed  = 0
    for etf in ALL_ETFS:
        # 隨機間隔避免被限流（3~8 秒）
        await asyncio.sleep(random.uniform(3, 8))
        try:
            data = await asyncio.to_thread(fetch_one_etf, etf['ticker'], etf['market'])
        except Exception as e:
            logger.warning(f"⚠️ {etf['ticker']} 抓取例外: {e}")
            data = None

        if not data:
            failed += 1
            logger.warning(f"⚠️ {etf['ticker']} 無法取得數據，本次略過（不用 mock）")
            continue

        try:
            with get_db() as (conn, cursor):
                cursor.execute("""
                    INSERT OR REPLACE INTO etf_daily_data
                    (ticker, date,
                     current_price, price_change, price_change_percent,
                     volume, asset_size, nav,
                     dividend_yield, payout_freq,
                     annual_return_1y, annual_return_3y, annual_return_5y,
                     pe_ratio, expense_ratio,
                     day_high, day_low,
                     fifty_two_week_high, fifty_two_week_low)
                    VALUES (%s,%s, %s,%s,%s, %s,%s,%s, %s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s)
                """, (
                    data['ticker'], today,
                    data['current_price'],   data['price_change'],   data['price_change_percent'],
                    data['volume'],          data['asset_size'],     data['nav'],
                    data['dividend_yield'],  data['payout_freq'],
                    data['annual_return_1y'],data['annual_return_3y'],data['annual_return_5y'],
                    data['pe_ratio'],        data['expense_ratio'],
                    data['day_high'],        data['day_low'],
                    data['fifty_two_week_high'], data['fifty_two_week_low'],
                ))
                conn.commit()
                updated += 1
        except Exception as e:
            logger.error(f"❌ 儲存 {etf['ticker']} 失敗: {e}")
            failed += 1

    logger.info(f"✅ 更新完成：成功 {updated} 檔，失敗/略過 {failed} 檔")


# ─────────────────────────────────────────────
# 認證工具
# ─────────────────────────────────────────────
def hash_password(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()

def verify_password(p: str, h: str) -> bool:
    return hash_password(p) == h

def generate_token() -> str:
    return secrets.token_hex(32)


# ─────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────
MAIN_LOOP = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global MAIN_LOOP
    MAIN_LOOP = asyncio.get_running_loop()
    init_db()
    insert_mock_data()
    start_scheduler()
    asyncio.create_task(_delayed_update())
    yield

async def _delayed_update():
    await asyncio.sleep(5)   # 5秒後立刻開始，不再等60秒
    await update_all_etf_data()

def _schedule_update():
    if MAIN_LOOP and MAIN_LOOP.is_running():
        asyncio.run_coroutine_threadsafe(update_all_etf_data(), MAIN_LOOP)

def start_scheduler():
    sch = BackgroundScheduler()
    sch.add_job(_schedule_update, CronTrigger(hour=14, minute=30))
    sch.add_job(_schedule_update, CronTrigger(hour=21, minute=0))
    sch.start()
    logger.info("排程器已啟動")


# ─────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────
app = FastAPI(title="ETF 投資管理系統", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory=TEMPLATES_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─────────────────────────────────────────────
# 共用 SQL helpers（抽出最新一筆 daily_data）
# ─────────────────────────────────────────────
LATEST_DAILY_JOIN = """
LEFT JOIN (
    SELECT d1.* FROM etf_daily_data d1
    INNER JOIN (
        SELECT ticker, MAX(date) AS max_date
        FROM etf_daily_data GROUP BY ticker
    ) d2 ON d1.ticker = d2.ticker AND d1.date = d2.max_date
) d ON m.ticker = d.ticker
"""


def _enrich(rows: list) -> list:
    for r in rows:
        r['price_change_percent'] = round(float(r.get('price_change_percent') or 0), 2)
        r['dividend_yield']       = round(float(r.get('dividend_yield') or 0), 2)
        r['annual_return_1y']     = round(float(r.get('annual_return_1y') or 0), 2)
        r['current_price']        = round(float(r.get('current_price') or 0), 2)
        r['volume']               = int(r.get('volume') or 0)
        r['asset_size']           = float(r.get('asset_size') or 0)
        r['payout_freq']          = r.get('payout_freq') or '季配'
    return rows


# ─────────────────────────────────────────────
# 頁面路由
# ─────────────────────────────────────────────
@app.get("/",             response_class=HTMLResponse)
async def root(req: Request):
    return templates.TemplateResponse("index.html", {"request": req})

@app.get("/etf-list",     response_class=HTMLResponse)
async def etf_list_page(req: Request):
    return templates.TemplateResponse("etf_list.html", {"request": req})

@app.get("/etf-detail/{ticker}", response_class=HTMLResponse)
async def etf_detail_page(req: Request, ticker: str):
    return templates.TemplateResponse("etf-detail.html", {"request": req, "ticker": ticker})

@app.get("/watchlist",    response_class=HTMLResponse)
async def watchlist_page(req: Request):
    return templates.TemplateResponse("watchlist.html", {"request": req})

@app.get("/portfolio",    response_class=HTMLResponse)
async def portfolio_page(req: Request):
    return templates.TemplateResponse("portfolio.html", {"request": req})

@app.get("/backtest",     response_class=HTMLResponse)
async def backtest_page(req: Request):
    return templates.TemplateResponse("backtest.html", {"request": req})

@app.get("/profile",      response_class=HTMLResponse)
async def profile_page(req: Request):
    return templates.TemplateResponse("profile.html", {"request": req})

@app.get("/auth",         response_class=HTMLResponse)
@app.get("/login",        response_class=HTMLResponse)
async def auth_page(req: Request):
    return templates.TemplateResponse("auth.html", {"request": req})


# ─────────────────────────────────────────────
# ETF API
# ─────────────────────────────────────────────
@app.get("/api/etf-rankings/{rank_type}")
async def get_etf_rankings(rank_type: str):
    ORDER = {
        "volume":    "COALESCE(d.volume,0) DESC",
        "asset":     "COALESCE(d.asset_size,0) DESC",
        "yield":     "COALESCE(d.dividend_yield,0) DESC",
        "return":    "COALESCE(d.annual_return_1y,0) DESC",
        "followers": "COALESCE(d.asset_size,0) DESC",
    }
    if rank_type not in ORDER:
        return safe_json({"status":"error","message":"無效類型"}, 400)
    try:
        with get_db() as (conn, cursor):
            cursor.execute(f"""
                SELECT m.ticker, m.name, m.market,
                    COALESCE(d.current_price,0) as current_price,
                    COALESCE(d.volume,0) as volume,
                    COALESCE(d.asset_size,0) as asset_size,
                    COALESCE(d.dividend_yield,0) as dividend_yield,
                    COALESCE(d.payout_freq,'季配') as payout_freq,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.annual_return_1y,0) as annual_return_1y,
                    0 as followers, 0 as hot_score
                FROM etf_master m {LATEST_DAILY_JOIN}
                ORDER BY {ORDER[rank_type]} LIMIT 30
            """)
            rows = cursor.fetchall()
        return safe_json({"status":"success","data":_enrich(rows),
                          "update_time": datetime.now().strftime("%Y-%m-%d %H:%M")})
    except Exception as e:
        logger.error(f"排名 API 錯誤: {e}")
        return safe_json({"status":"error","message":str(e)}, 500)


@app.get("/api/etf/search")
async def search_etf(q: str = Query(..., min_length=1)):
    try:
        like = f"%{q.upper()}%"
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT m.ticker, m.name, m.market,
                    COALESCE(d.current_price,0) as current_price,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.payout_freq,'季配') as payout_freq
                FROM etf_master m {join}
                WHERE UPPER(m.ticker) LIKE %s OR m.name LIKE %s
                ORDER BY m.ticker LIMIT 20
            """.format(join=LATEST_DAILY_JOIN), (like, f"%{q}%"))
            rows = cursor.fetchall()
        return safe_json({"status":"success","data":_enrich(rows)})
    except Exception as e:
        return safe_json({"status":"error","message":str(e)}, 500)


@app.get("/api/etf/search/dynamic")
async def dynamic_search_etf(q: str = Query(..., min_length=1)):
    """先查 DB，找不到再 yfinance 動態查詢"""
    results = []
    existing = set()

    try:
        like = f"%{q.upper()}%"
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT m.ticker, m.name, m.market,
                    COALESCE(d.current_price,0) as current_price,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.payout_freq,'季配') as payout_freq
                FROM etf_master m {join}
                WHERE UPPER(m.ticker) LIKE %s OR m.name LIKE %s
                ORDER BY m.ticker LIMIT 30
            """.format(join=LATEST_DAILY_JOIN), (like, f"%{q}%"))
            for r in cursor.fetchall():
                r['source'] = 'database'
                results.append(r)
                existing.add(r['ticker'])

        # 動態 yfinance 查詢（只試少量候選）
        if len(results) < 5:
            candidates = [f"{q.upper()}.TW", f"{q.upper()}.TWO", q.upper()]
            for yt in candidates:
                if yt.replace('.TW','').replace('.TWO','') in existing:
                    continue
                try:
                    stock = yf.Ticker(yt, session=_get_yf_session())
                    info = stock.fast_info
                    price = getattr(info, 'last_price', None) or 0
                    if price and price > 0:
                        market = 'TW' if '.TW' in yt else 'US'
                        display = yt.replace('.TW','').replace('.TWO','')
                        results.append({
                            'ticker': display, 'name': display,
                            'market': market, 'current_price': float(price),
                            'price_change_percent': 0.0, 'payout_freq': '季配',
                            'source': 'yfinance'
                        })
                        existing.add(display)
                except:
                    pass

        _enrich(results)
        return safe_json({"status":"success","data":results,"total":len(results)})
    except Exception as e:
        return safe_json({"status":"error","message":str(e)}, 500)


@app.post("/api/etf/add-to-master")
async def add_etf_to_master(request: Request):
    try:
        body = await request.json()
        ticker = body.get('ticker','').upper().strip()
        if not ticker:
            return safe_json({"status":"error","message":"請提供 ETF 代碼"}, 400)

        with get_db() as (conn, cursor):
            cursor.execute("SELECT ticker FROM etf_master WHERE ticker=%s", (ticker,))
            if cursor.fetchone():
                return safe_json({"status":"error","message":f"{ticker} 已存在"}, 400)

        market = 'TW' if ticker[:4].isdigit() else 'US'
        yt = _yahoo_ticker(ticker, market)
        name = ticker
        try:
            stock = yf.Ticker(yt, session=_get_yf_session())
            info = stock.info
            name = info.get('longName') or info.get('shortName') or ticker
            name = name[:200]
        except:
            pass

        with get_db() as (conn, cursor):
            cursor.execute("INSERT OR REPLACE INTO etf_master (ticker,name,market) VALUES (%s,%s,%s)",
                           (ticker, name, market))
            conn.commit()
        return safe_json({"status":"success","message":f"已新增 {ticker}",
                          "data":{"ticker":ticker,"name":name,"market":market}})
    except Exception as e:
        return safe_json({"status":"error","message":str(e)}, 500)


@app.get("/api/etf/detail/{ticker}")
async def get_etf_detail(ticker: str):
    ticker = ticker.upper()
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT m.ticker, m.name, m.market,
                    COALESCE(m.issuer,'') as issuer,
                    COALESCE(m.listing_date,'') as listing_date,
                    COALESCE(d.current_price,0) as current_price,
                    COALESCE(d.price_change,0) as price_change,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.volume,0) as volume,
                    COALESCE(d.asset_size,0) as asset_size,
                    COALESCE(d.nav,0) as nav,
                    COALESCE(d.dividend_yield,0) as dividend_yield,
                    COALESCE(d.payout_freq,'') as payout_freq,
                    COALESCE(d.annual_return_1y,0) as annual_return_1y,
                    COALESCE(d.annual_return_3y,0) as annual_return_3y,
                    COALESCE(d.annual_return_5y,0) as annual_return_5y,
                    COALESCE(d.pe_ratio,0) as pe_ratio,
                    COALESCE(d.expense_ratio,0) as expense_ratio,
                    COALESCE(d.day_high,0) as day_high,
                    COALESCE(d.day_low,0) as day_low,
                    COALESCE(d.fifty_two_week_high,0) as fifty_two_week_high,
                    COALESCE(d.fifty_two_week_low,0) as fifty_two_week_low,
                    d.date as data_date
                FROM etf_master m {join}
                WHERE m.ticker=%s
            """.format(join=LATEST_DAILY_JOIN), (ticker,))
            row = cursor.fetchone()

        if not row:
            return safe_json({"status":"error","message":"ETF 資料尚未抓取，請稍後再試（系統啟動後約 1 分鐘會自動更新）"}, 404)

        _enrich([row])
        return safe_json({"status":"success","data":row})
    except Exception as e:
        logger.error(f"detail API 錯誤 {ticker}: {e}")
        return safe_json({"status":"error","message":str(e)}, 500)


@app.get("/api/etf/price-history/{ticker}")
async def get_price_history(ticker: str, period: str = "1y"):
    ticker = ticker.upper()
    try:
        market = 'TW'
        with get_db() as (conn, cursor):
            cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
            r = cursor.fetchone()
            if r: market = r['market']

        # 將 period 換算為 range 參數
        range_map = {"1y": "1y", "3y": "3y", "5y": "5y", "6m": "6mo", "3m": "3mo"}
        yf_range = range_map.get(period, "1y")

        if market == 'TW':
            # 台灣證交所歷史（月均）
            closes = await asyncio.to_thread(_fetch_tw_history_twse, ticker)
            # 根據 period 截取
            n = {"1y":12,"3y":36,"5y":60,"6m":6,"3m":3}.get(period, 12)
            closes = closes[-n:] if len(closes) > n else closes
            if not closes:
                return safe_json({"status":"error","message":"無法取得歷史價格"}, 404)
            # 產生月份 labels
            labels = []
            now = datetime.now()
            for i in range(len(closes), 0, -1):
                dt = now - relativedelta(months=i-1)
                labels.append(dt.strftime('%Y/%m'))
            return safe_json({"status":"success","labels":labels,"prices":closes})
        else:
            # 美股 → Query2 REST
            url = (
                f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
                f"?range={yf_range}&interval=1mo&includePrePost=false"
            )
            def _get():
                s = _new_session()
                s.headers["Referer"] = f"https://finance.yahoo.com/quote/{ticker}"
                r2 = s.get(url, timeout=12)
                if r2.status_code != 200:
                    return None, None
                j = r2.json()
                result = j.get("chart", {}).get("result")
                if not result:
                    return None, None
                timestamps = result[0].get("timestamp", [])
                quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
                closes = quotes.get("close", [])
                labels = []
                prices = []
                for ts, c in zip(timestamps, closes):
                    if c is not None:
                        labels.append(datetime.fromtimestamp(ts).strftime('%Y/%m'))
                        prices.append(round(float(c), 2))
                return labels, prices

            labels, prices = await asyncio.to_thread(_get)
            if not labels:
                return safe_json({"status":"error","message":"無法取得歷史價格"}, 404)
            return safe_json({"status":"success","labels":labels,"prices":prices})

    except Exception as e:
        logger.error(f"price-history API 錯誤 {ticker}: {e}")
        return safe_json({"status":"error","message":str(e)}, 500)


@app.get("/api/etf/dividends/{ticker}")
async def get_dividends(ticker: str):
    ticker = ticker.upper()
    try:
        market = 'TW'
        with get_db() as (conn, cursor):
            cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
            r = cursor.fetchone()
            if r: market = r['market']

        if market == 'TW':
            # TWSE 配息
            div_yield, freq = await asyncio.to_thread(_fetch_tw_dividend_twse, ticker, 1.0)
            return safe_json({"status":"success","data":[],"dividend_yield":div_yield,"payout_freq":freq})
        else:
            # Query2 配息事件
            def _get_divs():
                url = (
                    f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}"
                    f"?range=3y&interval=1mo&events=dividends&includePrePost=false"
                )
                s = _new_session()
                s.headers["Referer"] = f"https://finance.yahoo.com/quote/{ticker}"
                r2 = s.get(url, timeout=10)
                if r2.status_code != 200:
                    return []
                j = r2.json()
                result = j.get("chart", {}).get("result")
                if not result:
                    return []
                events = result[0].get("events", {}).get("dividends", {})
                rows = [
                    {"date": datetime.fromtimestamp(v["date"]).strftime('%Y-%m-%d'),
                     "amount": round(float(v["amount"]), 4)}
                    for v in sorted(events.values(), key=lambda x: x["date"], reverse=True)
                    if v.get("amount", 0) > 0
                ]
                return rows[:20]
            data = await asyncio.to_thread(_get_divs)
            return safe_json({"status":"success","data":data})
    except Exception as e:
        return safe_json({"status":"success","data":[]})


@app.post("/api/etf/force-update")
async def force_update():
    asyncio.create_task(update_all_etf_data())
    return safe_json({"status":"success","message":"已啟動全量更新，約 10~15 分鐘完成"})


@app.post("/api/etf/update/{ticker}")
async def update_one_etf(ticker: str):
    """立即更新單一 ETF（供 detail 頁面呼叫）"""
    ticker = ticker.upper()
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
            r = cursor.fetchone()
        market = r['market'] if r else ('TW' if ticker[:4].isdigit() else 'US')

        # fetch_one_etf 內部已用 _new_session()，不需額外處理
        data = await asyncio.to_thread(fetch_one_etf, ticker, market)
        if not data:
            return safe_json({"status":"error","message":"無法取得數據，請稍後再試"}, 503)

        today = datetime.now().date()
        with get_db() as (conn, cursor):
            cursor.execute("""
                INSERT OR REPLACE INTO etf_daily_data
                (ticker, date,
                 current_price, price_change, price_change_percent,
                 volume, asset_size, nav,
                 dividend_yield, payout_freq,
                 annual_return_1y, annual_return_3y, annual_return_5y,
                 pe_ratio, expense_ratio,
                 day_high, day_low,
                 fifty_two_week_high, fifty_two_week_low)
                VALUES (%s,%s, %s,%s,%s, %s,%s,%s, %s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s)
            """, (
                data['ticker'], today,
                data['current_price'],    data['price_change'],    data['price_change_percent'],
                data['volume'],           data['asset_size'],      data['nav'],
                data['dividend_yield'],   data['payout_freq'],
                data['annual_return_1y'], data['annual_return_3y'],data['annual_return_5y'],
                data['pe_ratio'],         data['expense_ratio'],
                data['day_high'],         data['day_low'],
                data['fifty_two_week_high'], data['fifty_two_week_low'],
            ))
            conn.commit()
        return safe_json({"status":"success","message":f"{ticker} 更新完成","data":data})
    except Exception as e:
        logger.error(f"單一 ETF 更新錯誤 {ticker}: {e}")
        return safe_json({"status":"error","message":str(e)}, 500)


# ─────────────────────────────────────────────
# 用戶 API
# ─────────────────────────────────────────────
@app.post("/api/auth/register")
async def register(request: Request):
    try:
        body = await request.json()
        u = body.get('username','').strip()
        e = body.get('email','').strip().lower()
        p = body.get('password','')
        if len(u) < 3 or '@' not in e or len(p) < 6:
            return safe_json({"status":"error","message":"格式不符（用戶名至少3字元、信箱需含@、密碼至少6字元）"}, 400)
        with get_db() as (conn, cursor):
            cursor.execute("SELECT id FROM users WHERE email=%s OR username=%s", (e, u))
            if cursor.fetchone():
                return safe_json({"status":"error","message":"帳號或信箱已被註冊"}, 400)
            cursor.execute("INSERT INTO users (username,email,password_hash) VALUES (%s,%s,%s)",
                           (u, e, hash_password(p)))
            uid = cursor.lastrowid
            conn.commit()
        return safe_json({"status":"success","message":"註冊成功","user":{"id":uid,"username":u}})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.post("/api/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
        e = body.get('email','').strip().lower()
        p = body.get('password','')
        with get_db() as (conn, cursor):
            cursor.execute("SELECT id,username,password_hash,avatar FROM users WHERE email=%s", (e,))
            user = cursor.fetchone()
        if not user or not verify_password(p, user['password_hash']):
            return safe_json({"status":"error","message":"信箱或密碼錯誤"}, 401)
        return safe_json({"status":"success","token":generate_token(),
                          "user":{"id":user['id'],"username":user['username'],
                                  "avatar":user.get('avatar','') or ''}})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.post("/api/auth/change-password")
async def change_password(request: Request):
    try:
        body = await request.json()
        uid  = body.get('user_id')
        curr = body.get('current_password','')
        new  = body.get('new_password','')
        if len(new) < 6:
            return safe_json({"status":"error","message":"新密碼至少6字元"}, 400)
        with get_db() as (conn, cursor):
            cursor.execute("SELECT password_hash FROM users WHERE id=%s", (uid,))
            row = cursor.fetchone()
            if not row or not verify_password(curr, row['password_hash']):
                return safe_json({"status":"error","message":"目前密碼錯誤"}, 401)
            cursor.execute("UPDATE users SET password_hash=%s WHERE id=%s", (hash_password(new), uid))
            conn.commit()
        return safe_json({"status":"success","message":"密碼已更新"})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.get("/api/user/profile/{user_id}")
async def get_user_profile(user_id: int):
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT id,username,email,phone,avatar,created_at FROM users WHERE id=%s", (user_id,))
            user = cursor.fetchone()
        if not user:
            return safe_json({"status":"error","message":"用戶不存在"}, 404)
        if user.get('created_at') and hasattr(user['created_at'], 'strftime'):
            user['created_at'] = user['created_at'].strftime('%Y-%m-%d')
        return safe_json({"status":"success","data":user})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.put("/api/user/profile/{user_id}")
async def update_user_profile(user_id: int, request: Request):
    try:
        body = await request.json()
        u = body.get('username','').strip()
        e = body.get('email','').strip().lower()
        ph = body.get('phone','').strip()
        if len(u) < 3 or '@' not in e:
            return safe_json({"status":"error","message":"格式不符"}, 400)
        with get_db() as (conn, cursor):
            cursor.execute("UPDATE users SET username=%s,email=%s,phone=%s WHERE id=%s", (u,e,ph,user_id))
            conn.commit()
        return safe_json({"status":"success","message":"更新成功"})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.post("/api/user/avatar/{user_id}")
async def upload_avatar(user_id: int, file: UploadFile = File(...)):
    try:
        if not file.content_type.startswith('image/'):
            return safe_json({"status":"error","message":"請上傳圖片"}, 400)
        ext = file.filename.split('.')[-1]
        fname = f"avatar_{user_id}_{int(time.time())}.{ext}"
        fpath = os.path.join(AVATAR_DIR, fname)
        with open(fpath, "wb") as buf:
            shutil.copyfileobj(file.file, buf)
        url = f"/static/uploads/avatars/{fname}"
        with get_db() as (conn, cursor):
            cursor.execute("UPDATE users SET avatar=%s WHERE id=%s", (url, user_id))
            conn.commit()
        return safe_json({"status":"success","avatar_url":url})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.delete("/api/user/delete")
async def delete_user(request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"error","message":"請先登入"}, 401)
    try:
        with get_db() as (conn, cursor):
            for tbl in ['user_transactions','user_watchlist','user_portfolio']:
                cursor.execute(f"DELETE FROM {tbl} WHERE user_id=%s", (uid,))
            cursor.execute("DELETE FROM users WHERE id=%s", (uid,))
            conn.commit()
        return safe_json({"status":"success","message":"帳號已刪除"})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


# ─────────────────────────────────────────────
# 自選股 API
# ─────────────────────────────────────────────
@app.get("/api/watchlist")
async def get_watchlist(request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"success","data":[]})
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT w.ticker, m.name, m.market,
                    COALESCE(d.current_price,0) as current_price,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.payout_freq,'季配') as payout_freq,
                    COALESCE(d.volume,0) as volume,
                    COALESCE(d.day_high,0) as day_high,
                    COALESCE(d.day_low,0) as day_low,
                    COALESCE(d.dividend_yield,0) as dividend_yield
                FROM user_watchlist w
                JOIN etf_master m ON w.ticker=m.ticker
                LEFT JOIN (
                    SELECT d1.* FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) AS max_date FROM etf_daily_data GROUP BY ticker
                    ) d2 ON d1.ticker=d2.ticker AND d1.date=d2.max_date
                ) d ON w.ticker=d.ticker
                WHERE w.user_id=%s ORDER BY w.added_at DESC
            """, (uid,))
            rows = cursor.fetchall()
        return safe_json({"status":"success","data":_enrich(rows)})
    except Exception as ex:
        logger.error(f"watchlist 錯誤: {ex}")
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.post("/api/watchlist/add")
async def add_watchlist(request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"error","message":"請先登入"}, 401)
    try:
        body = await request.json()
        ticker = body.get('ticker','').upper().strip()
        name   = body.get('name', ticker)
        market = body.get('market', 'TW' if ticker[:4].isdigit() else 'US')

        with get_db() as (conn, cursor):
            # 若 ETF 主檔不存在，先建立
            cursor.execute("SELECT ticker FROM etf_master WHERE ticker=%s", (ticker,))
            if not cursor.fetchone():
                cursor.execute("INSERT OR REPLACE INTO etf_master (ticker,name,market) VALUES (%s,%s,%s)",
                               (ticker, name, market))

            cursor.execute("SELECT id FROM user_watchlist WHERE user_id=%s AND ticker=%s", (uid, ticker))
            if cursor.fetchone():
                return safe_json({"status":"error","message":"已在自選清單中"}, 400)

            cursor.execute("INSERT INTO user_watchlist (user_id,ticker) VALUES (%s,%s)", (uid, ticker))
            conn.commit()
        return safe_json({"status":"success","message":f"已加入自選：{ticker}"})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.delete("/api/watchlist/remove/{ticker}")
async def remove_watchlist(ticker: str, request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"error","message":"請先登入"}, 401)
    try:
        with get_db() as (conn, cursor):
            cursor.execute("DELETE FROM user_watchlist WHERE user_id=%s AND ticker=%s",
                           (uid, ticker.upper()))
            conn.commit()
        return safe_json({"status":"success","message":"已移除"})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


# ─────────────────────────────────────────────
# 庫存 / 交易 API
# ─────────────────────────────────────────────
@app.get("/api/portfolio")
async def get_portfolio(request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"success","data":[],"summary":{"total_cost":0,"total_value":0,"total_profit":0,"total_return":0}})
    try:
        with get_db() as (conn, cursor):
            cursor.execute("""
                SELECT p.ticker, m.name, m.market,
                    p.shares, p.avg_cost,
                    COALESCE(d.current_price, p.avg_cost) as current_price,
                    COALESCE(d.price_change_percent,0) as price_change_percent,
                    COALESCE(d.dividend_yield,0) as dividend_yield
                FROM user_portfolio p
                JOIN etf_master m ON p.ticker=m.ticker
                LEFT JOIN (
                    SELECT d1.* FROM etf_daily_data d1
                    INNER JOIN (
                        SELECT ticker, MAX(date) AS max_date FROM etf_daily_data GROUP BY ticker
                    ) d2 ON d1.ticker=d2.ticker AND d1.date=d2.max_date
                ) d ON p.ticker=d.ticker
                WHERE p.user_id=%s AND p.shares>0
                ORDER BY p.ticker
            """.format(join=LATEST_DAILY_JOIN), (uid,))
            rows = cursor.fetchall()

        total_cost = 0; total_value = 0
        for r in rows:
            r['shares']        = float(r['shares'])
            r['avg_cost']      = float(r['avg_cost'])
            r['current_price'] = float(r['current_price'])
            r['cost']          = round(r['shares'] * r['avg_cost'], 2)
            r['market_value']  = round(r['shares'] * r['current_price'], 2)
            r['profit']        = round(r['market_value'] - r['cost'], 2)
            r['return_pct']    = round((r['profit'] / r['cost'] * 100) if r['cost'] > 0 else 0, 2)
            r['price_change_percent'] = round(float(r['price_change_percent']), 2)
            r['dividend_yield']       = round(float(r['dividend_yield']), 2)
            total_cost  += r['cost']
            total_value += r['market_value']

        total_profit = round(total_value - total_cost, 2)
        total_return = round(total_profit / total_cost * 100 if total_cost > 0 else 0, 2)
        return safe_json({"status":"success","data":rows,"summary":{
            "total_cost": round(total_cost,2),
            "total_value": round(total_value,2),
            "total_profit": total_profit,
            "total_return": total_return,
        }})
    except Exception as ex:
        logger.error(f"portfolio 錯誤: {ex}")
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.post("/api/portfolio/transaction")
async def add_transaction(request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"error","message":"請先登入"}, 401)
    try:
        body = await request.json()
        ticker = body.get('ticker','').upper().strip()
        ttype  = body.get('transaction_type','buy').lower()
        shares = float(body.get('shares', 0))
        price  = float(body.get('price', 0))
        commission = float(body.get('commission', 0))
        tx_date = body.get('transaction_date', datetime.now().strftime('%Y-%m-%d'))
        name   = body.get('name', ticker)
        market = body.get('market', 'TW' if ticker[:4].isdigit() else 'US')

        if shares <= 0 or price <= 0:
            return safe_json({"status":"error","message":"股數和價格必須大於0"}, 400)

        with get_db() as (conn, cursor):
            # 確保 ETF 主檔存在
            cursor.execute("SELECT ticker FROM etf_master WHERE ticker=%s", (ticker,))
            if not cursor.fetchone():
                cursor.execute("INSERT OR REPLACE INTO etf_master (ticker,name,market) VALUES (%s,%s,%s)",
                               (ticker, name, market))

            if ttype == 'sell':
                cursor.execute("SELECT shares FROM user_portfolio WHERE user_id=%s AND ticker=%s", (uid, ticker))
                row = cursor.fetchone()
                if not row or float(row['shares']) < shares:
                    return safe_json({"status":"error","message":"持股不足"}, 400)

            # 記錄交易
            cursor.execute("""
                INSERT INTO user_transactions
                (user_id,ticker,transaction_type,shares,price,commission,transaction_date)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (uid, ticker, ttype, shares, price, commission, tx_date))

            # 重新計算 portfolio
            cursor.execute("""
                SELECT
                    SUM(CASE WHEN transaction_type='buy' THEN shares ELSE -shares END) as total_shares,
                    SUM(CASE WHEN transaction_type='buy' THEN shares*price ELSE 0 END) as total_cost,
                    SUM(CASE WHEN transaction_type='buy' THEN shares ELSE 0 END) as buy_shares
                FROM user_transactions WHERE user_id=%s AND ticker=%s
            """, (uid, ticker))
            agg = cursor.fetchone()
            total_shares = float(agg['total_shares'] or 0)
            total_cost   = float(agg['total_cost'] or 0)
            buy_shares   = float(agg['buy_shares'] or 0)

            if total_shares < 0.001:
                cursor.execute("DELETE FROM user_portfolio WHERE user_id=%s AND ticker=%s", (uid, ticker))
            else:
                avg_cost = total_cost / buy_shares if buy_shares > 0 else price
                cursor.execute("""
                    INSERT OR REPLACE INTO user_portfolio (user_id,ticker,shares,avg_cost)
                    VALUES (%s,%s,%s,%s)
                """, (uid, ticker, total_shares, avg_cost))
            conn.commit()

        return safe_json({"status":"success","message":"交易已記錄"})
    except Exception as ex:
        logger.error(f"交易 API 錯誤: {ex}")
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.get("/api/portfolio/transactions")
async def get_transactions(request: Request, ticker: str = None):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"success","data":[]})
    try:
        with get_db() as (conn, cursor):
            if ticker:
                cursor.execute("""
                    SELECT t.*, m.name FROM user_transactions t
                    JOIN etf_master m ON t.ticker=m.ticker
                    WHERE t.user_id=%s AND t.ticker=%s ORDER BY t.transaction_date DESC
                """, (uid, ticker.upper()))
            else:
                cursor.execute("""
                    SELECT t.*, m.name FROM user_transactions t
                    JOIN etf_master m ON t.ticker=m.ticker
                    WHERE t.user_id=%s ORDER BY t.transaction_date DESC LIMIT 100
                """, (uid,))
            rows = cursor.fetchall()
        for r in rows:
            for f in ['shares','price','commission']:
                r[f] = float(r.get(f) or 0)
            if r.get('transaction_date') and hasattr(r['transaction_date'], 'strftime'):
                r['transaction_date'] = r['transaction_date'].strftime('%Y-%m-%d')
        return safe_json({"status":"success","data":rows})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


@app.delete("/api/portfolio/transaction/{tid}")
async def delete_transaction(tid: int, request: Request):
    uid = request.headers.get('X-User-Id')
    if not uid:
        return safe_json({"status":"error","message":"請先登入"}, 401)
    try:
        with get_db() as (conn, cursor):
            cursor.execute("SELECT * FROM user_transactions WHERE id=%s AND user_id=%s", (tid, uid))
            tx = cursor.fetchone()
            if not tx:
                return safe_json({"status":"error","message":"交易記錄不存在"}, 404)
            ticker = tx['ticker']
            cursor.execute("DELETE FROM user_transactions WHERE id=%s", (tid,))

            cursor.execute("""
                SELECT
                    SUM(CASE WHEN transaction_type='buy' THEN shares ELSE -shares END) as total_shares,
                    SUM(CASE WHEN transaction_type='buy' THEN shares*price ELSE 0 END) as total_cost,
                    SUM(CASE WHEN transaction_type='buy' THEN shares ELSE 0 END) as buy_shares
                FROM user_transactions WHERE user_id=%s AND ticker=%s
            """, (uid, ticker))
            agg = cursor.fetchone()
            total_shares = float(agg['total_shares'] or 0) if agg else 0

            if total_shares < 0.001:
                cursor.execute("DELETE FROM user_portfolio WHERE user_id=%s AND ticker=%s", (uid, ticker))
            else:
                buy_shares = float(agg['buy_shares'] or 0)
                total_cost = float(agg['total_cost'] or 0)
                avg_cost = total_cost / buy_shares if buy_shares > 0 else 0
                cursor.execute("INSERT OR REPLACE INTO user_portfolio (user_id,ticker,shares,avg_cost) VALUES (%s,%s,%s,%s)",
                               (uid, ticker, total_shares, avg_cost))
            conn.commit()
        return safe_json({"status":"success","message":"已刪除"})
    except Exception as ex:
        return safe_json({"status":"error","message":str(ex)}, 500)


# ─────────────────────────────────────────────
# 回測 API
# ─────────────────────────────────────────────
@app.post("/api/backtest")
async def run_backtest(request: Request):
    try:
        body = await request.json()
        mode       = body.get('mode', 'accumulate')
        ticker     = body.get('ticker', '0050').upper()
        start_date = body.get('start_date', '2020-01-01')
        end_date   = body.get('end_date',   '2024-12-31')

        market = 'TW'
        with get_db() as (conn, cursor):
            cursor.execute("SELECT market FROM etf_master WHERE ticker=%s", (ticker,))
            r = cursor.fetchone()
            if r: market = r['market']

        yt = _yahoo_ticker(ticker, market)

        # ── 用 Query2 REST API 抓日線歷史（不用 yfinance.history，避免 rate limit）──
        def _get_backtest_hist():
            url = (
                f"https://query2.finance.yahoo.com/v8/finance/chart/{yt}"
                f"?period1={int(datetime.strptime(start_date,'%Y-%m-%d').timestamp())}"
                f"&period2={int(datetime.strptime(end_date,'%Y-%m-%d').timestamp())}"
                f"&interval=1mo&includePrePost=false"
            )
            s = _new_session()
            s.headers["Referer"] = f"https://finance.yahoo.com/quote/{yt}"
            r = s.get(url, timeout=15)
            if r.status_code != 200:
                return pd.DataFrame()
            j = r.json()
            result = j.get("chart", {}).get("result")
            if not result:
                return pd.DataFrame()
            timestamps = result[0].get("timestamp", [])
            quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
            closes = quotes.get("close", [])
            rows = []
            for ts, c in zip(timestamps, closes):
                if c is not None:
                    rows.append({"date": pd.Timestamp.fromtimestamp(ts), "Close": float(c)})
            if not rows:
                return pd.DataFrame()
            df = pd.DataFrame(rows).set_index("date")
            return df

        hist = await asyncio.to_thread(_get_backtest_hist)

        # 備援：台股用 TWSE 月均
        if (hist.empty or len(hist) < 3) and market == 'TW':
            def _get_tw_hist():
                closes = _fetch_tw_history_twse(ticker)
                if not closes:
                    return pd.DataFrame()
                now = datetime.now()
                rows = []
                start_dt = datetime.strptime(start_date, '%Y-%m-%d')
                end_dt   = datetime.strptime(end_date,   '%Y-%m-%d')
                for i, c in enumerate(closes):
                    dt = now - relativedelta(months=len(closes)-1-i)
                    if start_dt <= dt <= end_dt:
                        rows.append({"date": pd.Timestamp(dt), "Close": c})
                if not rows:
                    return pd.DataFrame()
                return pd.DataFrame(rows).set_index("date")
            hist = await asyncio.to_thread(_get_tw_hist)

        if hist.empty:
            return safe_json({"status":"error","message":"該時段無歷史數據，請換一個代碼或縮短日期範圍"}, 400)
        if hist.index.tz is not None:
            hist.index = hist.index.tz_localize(None)

        transactions = []
        total_invested = 0.0
        total_shares   = 0.0
        is_bankrupt    = False
        current_date   = pd.to_datetime(start_date)
        end            = pd.to_datetime(end_date)

        if mode == 'accumulate':
            ini_amt = float(body.get('initial_amount', 0))
            mon_amt = float(body.get('monthly_amount', 10000))
            if ini_amt > 0:
                p = float(hist['Close'].iloc[0])
                if p > 0:
                    total_invested += ini_amt
                    total_shares   += ini_amt / p
                    transactions.append({'date': hist.index[0].strftime('%Y-%m-%d'),
                                         'type':'期初單筆','amount':round(ini_amt,2),
                                         'price':round(p,2),'total_shares':round(total_shares,4),
                                         'market_value':round(total_shares*p,2)})
            while current_date <= end:
                month_data = hist[hist.index >= current_date]
                if not month_data.empty and mon_amt > 0:
                    p = float(month_data['Close'].iloc[0])
                    if p > 0:
                        total_invested += mon_amt
                        total_shares   += mon_amt / p
                        transactions.append({'date': month_data.index[0].strftime('%Y-%m-%d'),
                                             'type':'定期定額','amount':round(mon_amt,2),
                                             'price':round(p,2),'total_shares':round(total_shares,4),
                                             'market_value':round(total_shares*p,2)})
                current_date += relativedelta(months=1)

        elif mode == 'withdraw':
            init_val = float(body.get('withdraw_initial', 10000000))
            mon_wd   = float(body.get('withdraw_monthly',    40000))
            p_start  = float(hist['Close'].iloc[0])
            if p_start > 0:
                total_invested = init_val
                total_shares   = init_val / p_start
                transactions.append({'date': hist.index[0].strftime('%Y-%m-%d'),
                                     'type':'投入本金','amount':round(init_val,2),
                                     'price':round(p_start,2),'total_shares':round(total_shares,4),
                                     'market_value':round(init_val,2)})
            next_date = current_date + relativedelta(months=1)
            while next_date <= end and not is_bankrupt:
                month_data = hist[hist.index >= next_date]
                if not month_data.empty and mon_wd > 0:
                    p = float(month_data['Close'].iloc[0])
                    need = mon_wd / p
                    if total_shares >= need - 0.0001:
                        total_shares -= need
                        transactions.append({'date': month_data.index[0].strftime('%Y-%m-%d'),
                                             'type':'每月提領','amount':round(mon_wd,2),
                                             'price':round(p,2),'total_shares':round(total_shares,4),
                                             'market_value':round(total_shares*p,2)})
                    else:
                        transactions.append({'date': month_data.index[0].strftime('%Y-%m-%d'),
                                             'type':'💀 資產枯竭','amount':round(total_shares*p,2),
                                             'price':round(p,2),'total_shares':0,'market_value':0})
                        total_shares = 0; is_bankrupt = True; break
                next_date += relativedelta(months=1)

        final_price = float(hist['Close'].iloc[-1])
        final_value = total_shares * final_price
        if mode == 'accumulate':
            total_profit = final_value - total_invested
        else:
            withdrawn = sum(t['amount'] for t in transactions if '提領' in t['type'] or '枯竭' in t['type'])
            total_profit = (final_value + withdrawn) - total_invested

        return safe_json({"status":"success","data":{
            "mode": mode, "is_bankrupt": is_bankrupt,
            "total_invested": round(total_invested,2),
            "final_value": round(final_value,2),
            "total_profit": round(total_profit,2),
            "total_return": round(total_profit/total_invested*100 if total_invested > 0 else 0, 2),
            "final_price": round(final_price,2),
            "total_shares": round(total_shares,4),
            "transactions": transactions[-60:] if len(transactions) > 60 else transactions,
        }})
    except Exception as ex:
        logger.error(f"回測錯誤: {ex}")
        return safe_json({"status":"error","message":str(ex)}, 500)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
