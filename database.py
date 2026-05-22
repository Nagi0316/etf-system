"""
database.py — 資料庫連線管理、初始化、Schema Migration
支援 TiDB Cloud (MySQL 8) 及本地 SQLite 自動切換
"""
import time, logging
from contextlib import contextmanager
from typing import Optional
from config import (
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
    SQLITE_PATH, USE_MYSQL
)

logger = logging.getLogger(__name__)
_DB_RETRIES = 3


# ══════════════════════════════════════════════════════════
#  連線建立
# ══════════════════════════════════════════════════════════

def _get_mysql_conn():
    import mysql.connector, certifi
    params = dict(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASSWORD,
        database=DB_NAME,
        connection_timeout=15,
        autocommit=False,
        charset="utf8mb4",
        collation="utf8mb4_unicode_ci",
        use_unicode=True,
    )
    if "tidbcloud.com" in DB_HOST or str(DB_PORT) == "4000":
        params.update(
            ssl_ca=certifi.where(),
            ssl_verify_cert=True,
            ssl_verify_identity=True,
        )
    return mysql.connector.connect(**params)


def _get_sqlite_conn():
    import sqlite3
    conn = sqlite3.connect(SQLITE_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ══════════════════════════════════════════════════════════
#  統一 Cursor 包裝 (抹平 MySQL / SQLite 差異)
# ══════════════════════════════════════════════════════════

class DbCursor:
    def __init__(self, raw_cursor, is_mysql: bool):
        self._c = raw_cursor
        self._is_mysql = is_mysql
        self.lastrowid: Optional[int] = None

    def execute(self, sql: str, params=()):
        if self._is_mysql:
            if "INSERT OR REPLACE INTO" in sql.upper():
                sql = sql.replace("INSERT OR REPLACE INTO", "REPLACE INTO", 1)
            elif "INSERT OR IGNORE INTO" in sql.upper():
                sql = sql.replace("INSERT OR IGNORE INTO", "INSERT IGNORE INTO", 1)
        else:
            sql = sql.replace("%s", "?")
            up = sql.upper().lstrip()
            if "ON DUPLICATE KEY UPDATE" in up:
                idx = up.index("ON DUPLICATE KEY UPDATE")
                sql = sql[:idx].strip().rstrip(",")
                sql = sql.replace("INSERT INTO", "INSERT OR REPLACE INTO", 1)
            elif up.startswith("INSERT INTO"):
                sql = sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
            elif up.startswith("REPLACE INTO"):
                sql = sql.replace("REPLACE INTO", "INSERT OR REPLACE INTO", 1)
        self._c.execute(sql, params if params else ())
        self.lastrowid = self._c.lastrowid

    def fetchone(self):
        row = self._c.fetchone()
        if row is None:
            return None
        return dict(row) if not self._is_mysql else row

    def fetchall(self):
        rows = self._c.fetchall()
        return [dict(r) for r in rows] if not self._is_mysql else rows

    def close(self):
        try:
            self._c.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════
#  Context Manager
# ══════════════════════════════════════════════════════════

@contextmanager
def get_db():
    raw_conn = None
    conn_ctx = None
    cursor_obj = None
    last_err = None

    for attempt in range(_DB_RETRIES):
        try:
            if USE_MYSQL:
                raw_conn = _get_mysql_conn()
                raw_cur = raw_conn.cursor(dictionary=True)
                cursor_obj = DbCursor(raw_cur, is_mysql=True)

                class _MCtx:
                    def commit(self):   raw_conn.commit()
                    def rollback(self): raw_conn.rollback()
                    def close(self):
                        try: raw_conn.close()
                        except: pass
                conn_ctx = _MCtx()
            else:
                raw_conn = _get_sqlite_conn()
                raw_cur = raw_conn.cursor()
                cursor_obj = DbCursor(raw_cur, is_mysql=False)

                class _SCtx:
                    def commit(self):   raw_conn.commit()
                    def rollback(self): raw_conn.rollback()
                    def close(self):
                        try: raw_conn.close()
                        except: pass
                conn_ctx = _SCtx()
            break
        except Exception as e:
            last_err = e
            wait = 1.5 * (2 ** attempt)
            logger.warning(f"DB 連線失敗 ({attempt+1}/{_DB_RETRIES}): {e}，{wait:.1f}s 後重試")
            time.sleep(wait)

    if conn_ctx is None:
        raise ConnectionError(f"無法連線資料庫（已重試 {_DB_RETRIES} 次）: {last_err}")

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


# ══════════════════════════════════════════════════════════
#  Schema 初始化 (冪等)
# ══════════════════════════════════════════════════════════

def init_db():
    is_sqlite = not USE_MYSQL
    pk_auto   = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite else "INT AUTO_INCREMENT PRIMARY KEY"
    ts_now    = "DATETIME DEFAULT CURRENT_TIMESTAMP" if is_sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
    ts_upd    = "DATETIME DEFAULT CURRENT_TIMESTAMP" if is_sqlite else "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
    engine    = "" if is_sqlite else " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"

    if USE_MYSQL:
        import mysql.connector, certifi
        try:
            params = dict(host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD,
                          connection_timeout=10)
            if "tidbcloud.com" in DB_HOST:
                params.update(ssl_ca=certifi.where(), ssl_verify_cert=True, ssl_verify_identity=True)
            tmp = mysql.connector.connect(**params)
            tmp.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            tmp.commit(); tmp.close()
        except Exception as e:
            logger.debug(f"CREATE DATABASE 略過: {e}")

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
        ){engine}""",

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
            dividend_yield       DECIMAL(5,4)   DEFAULT NULL,
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
            discount_premium     DECIMAL(10,2)  DEFAULT 0,
            created_at           {ts_now},
            UNIQUE (ticker, date)
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS users (
            id             {pk_auto},
            username       VARCHAR(100) NOT NULL,
            email          VARCHAR(100) NOT NULL,
            password_hash  VARCHAR(255),
            phone          VARCHAR(20),
            avatar         VARCHAR(500),
            google_id      VARCHAR(255),
            google_name    VARCHAR(255),
            google_picture VARCHAR(500),
            auth_provider  VARCHAR(20)  DEFAULT 'google',
            monthly_budget DECIMAL(12,2) DEFAULT 10000,
            created_at     {ts_now},
            UNIQUE (email)
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS user_sessions (
            id         {pk_auto},
            user_id    INT         NOT NULL,
            jti        VARCHAR(64) NOT NULL,
            expires_at DATETIME    NOT NULL,
            is_revoked TINYINT(1)  DEFAULT 0,
            created_at {ts_now},
            UNIQUE (jti)
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS user_watchlist (
            id       {pk_auto},
            user_id  INT         NOT NULL,
            ticker   VARCHAR(20) NOT NULL,
            added_at {ts_now},
            UNIQUE (user_id, ticker)
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS user_transactions (
            id               {pk_auto},
            user_id          INT            NOT NULL,
            ticker           VARCHAR(20)    NOT NULL,
            transaction_type VARCHAR(10)    NOT NULL,
            shares           DECIMAL(10,4)  NOT NULL,
            price            DECIMAL(10,2)  NOT NULL,
            commission       DECIMAL(10,2)  DEFAULT 0,
            transaction_date DATE           NOT NULL,
            note             VARCHAR(500),
            created_at       {ts_now}
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS user_portfolio (
            id         {pk_auto},
            user_id    INT            NOT NULL,
            ticker     VARCHAR(20)    NOT NULL,
            shares     DECIMAL(10,4)  NOT NULL DEFAULT 0,
            avg_cost   DECIMAL(10,2)  NOT NULL DEFAULT 0,
            updated_at {ts_upd},
            UNIQUE (user_id, ticker)
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS notifications (
            id         {pk_auto},
            user_id    INT          NOT NULL,
            type       VARCHAR(50)  NOT NULL,
            title      VARCHAR(200) NOT NULL,
            content    TEXT         NOT NULL,
            ticker     VARCHAR(20),
            extra_data TEXT,
            is_read    TINYINT(1)   DEFAULT 0,
            created_at {ts_now}
        ){engine}""",

        f"""CREATE TABLE IF NOT EXISTS price_alerts (
            id           {pk_auto},
            user_id      INT            NOT NULL,
            ticker       VARCHAR(20)    NOT NULL,
            alert_type   VARCHAR(20)    NOT NULL,
            target_price DECIMAL(10,2)  NOT NULL,
            is_triggered TINYINT(1)     DEFAULT 0,
            is_active    TINYINT(1)     DEFAULT 1,
            created_at   {ts_now}
        ){engine}""",
    ]

    with get_db() as (conn, cursor):
        for ddl in ddls:
            try:
                cursor.execute(ddl)
            except Exception as e:
                logger.debug(f"DDL 略過: {e}")
        conn.commit()

    # 平滑升級舊資料庫（新增欄位，若已存在則略過）
    new_cols = [
        ("etf_daily_data", "discount_premium",   "DECIMAL(10,2) DEFAULT 0"),
        ("etf_daily_data", "annual_return_3y",   "DECIMAL(7,4)  DEFAULT 0"),
        ("etf_daily_data", "annual_return_5y",   "DECIMAL(7,4)  DEFAULT 0"),
        ("etf_daily_data", "fifty_two_week_high","DECIMAL(10,2) DEFAULT 0"),
        ("etf_daily_data", "fifty_two_week_low", "DECIMAL(10,2) DEFAULT 0"),
        ("etf_master",     "issuer",             "VARCHAR(100)"),
        ("etf_master",     "listing_date",       "DATE"),
        ("users",          "google_id",          "VARCHAR(255)"),
        ("users",          "google_name",        "VARCHAR(255)"),
        ("users",          "google_picture",     "VARCHAR(500)"),
        ("users",          "auth_provider",      "VARCHAR(20) DEFAULT 'google'"),
        ("users",          "monthly_budget",     "DECIMAL(12,2) DEFAULT 10000"),
        ("user_transactions", "note",            "VARCHAR(500)"),
        ("etf_master",     "is_hot",            "TINYINT(1) DEFAULT 0"),
        ("etf_master",     "auto_discovered",   "TINYINT(1) DEFAULT 0"),
        ("etf_master",     "is_delisted",       "TINYINT(1) DEFAULT 0"),
    ]
    with get_db() as (conn, cursor):
        for tbl, col, coldef in new_cols:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {coldef}")
                conn.commit()
                logger.info(f"✅ 新增欄位 {tbl}.{col}")
            except Exception:
                pass

    logger.info(f"✅ 資料庫初始化完成 ({'TiDB/MySQL' if USE_MYSQL else 'SQLite'})")
