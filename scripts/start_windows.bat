@echo off
chcp 65001 >nul
cd /d "%~dp0.."
color 0B

echo ===================================================
echo          ETF 投資系統 - 伺服器啟動器
echo ===================================================
echo.

where python >nul 2>&1
if errorlevel 1 (
    echo [錯誤] 找不到 Python，請先安裝 Python 3.12 以上版本。
    pause
    exit /b 1
)

if exist .env (
    echo [訊息] 偵測到 .env，應用程式會自動載入設定。
) else (
    echo [提示] 未找到 .env，將使用本機 SQLite。
    echo        TiDB: 將 .env.example 複製為 .env 並填入設定。
)

echo.
echo [訊息] 檢查套件...
python -c "import fastapi, uvicorn, jinja2, pandas, yfinance" >nul 2>&1
if errorlevel 1 (
    echo [訊息] 首次啟動，安裝必要套件中...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [錯誤] 套件安裝失敗。
        pause
        exit /b 1
    )
)

echo.
echo ===================================================
echo [資料庫] 依 .env 設定連線；未設定時使用 etf_tracker.db。
echo  👉 瀏覽器開啟：http://127.0.0.1:8000
echo ===================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

echo.
echo [錯誤] 伺服器已停止。
pause
