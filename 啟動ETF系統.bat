@echo off
chcp 65001 >nul
color 0B

echo ===================================================
echo          ETF 投資系統 - 伺服器啟動器
echo ===================================================
echo.

if exist .env (
    echo [訊息] 偵測到 .env，載入 TiDB 設定...
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A:~0,1%" == "#" if not "%%A" == "" (
            set "%%A=%%B"
        )
    )
) else (
    echo [提示] 未找到 .env，使用本機 SQLite
    echo        TiDB: 將 .env.example 複製為 .env 並填入設定
)

echo.
echo [訊息] 安裝套件中...
pip install -r requirements.txt -q

echo.
echo ===================================================
if defined DB_HOST (
    echo [資料庫] TiDB Cloud: %DB_HOST%
) else (
    echo [資料庫] 本地 SQLite: etf_tracker.db
)
echo  👉 瀏覽器開啟：http://127.0.0.1:8000
echo ===================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

echo.
echo [錯誤] 伺服器已停止。
pause
