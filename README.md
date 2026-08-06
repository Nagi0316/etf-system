# ETF 投資管理系統

台股與美股 ETF 的報價、排行、殖利率、價格走勢、回測、自選清單與投資組合管理系統。後端使用 FastAPI，前端為 Jinja2 + Tailwind CSS + Chart.js，資料庫支援 TiDB/MySQL 與本機 SQLite。

## 專案結構

```text
routes/                 HTTP 頁面與 API 路由
services/               回測、評分、匯率、歷史價與通知邏輯
static/                 編譯後 CSS、共用 JavaScript 與 Chart.js
templates/              Jinja2 頁面模板
tests/                  單元、資料流程與頁面完整性測試
database.py             Schema、migration 與連線管理
db_queries.py           跨功能共用的查詢片段
etf_data.py             ETF 目錄、報價與配息資料整合
scheduler.py            定時同步與修復作業
main.py                 FastAPI 應用、啟動週期與健康檢查
```

## 本機啟動

1. 安裝 Python 3.12 以上版本與 Node.js。
2. 建立虛擬環境後安裝依賴：

   ```bash
   python3 -m pip install -r requirements.txt
   npm ci
   ```

3. 複製 `.env.example` 為 `.env`，填入資料庫、JWT 與 OAuth 設定。`.env` 含有秘密，不得提交。
4. 啟動：

   ```bash
   python3 -m uvicorn main:app --reload
   ```

Windows 也可執行 `啟動ETF系統.bat`。未設定 TiDB 時會自動使用本機 SQLite。

## 前端建置

部署直接提供版本庫內的壓縮 CSS 與 Chart.js，因此修改 Tailwind 類別後應重新建置：

```bash
npm run build
npm run check:js
```

## 測試與診斷

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q . -x '(^|/)(node_modules|\.git)/'
python3 diagnose.py
```

測試範圍包含報價來源、價格歷史、分割還原、資料同步狀態、快取、公開頁面渲染與靜態資產完整性。

## 資料正確性原則

- 台股報價優先使用證交所/櫃買中心公開資料，美股與歷史價設有多來源備援。
- 價格資料保留交易日與來源；健康檢查會回報覆蓋率與最後成功時間。
- 殖利率區分 `confirmed`、`estimated`、`not_applicable` 與 `unknown`，不把缺少資料誤當成 0%。
- 配息缺口採持久化公平佇列分批補齊；來源失敗只記錄嘗試狀態，不會寫入猜測值，也不會讓同一批失敗標的阻塞後續代碼。
- 盤中全市場同步只執行有硬逾時的批量來源；較慢的逐檔備援由詳情頁或收盤後作業處理，避免排程重疊拖慢網站。
- 已下市或已確認無效標的不參與排行與日常同步。

本系統用於資訊整合與學術展示，不構成投資建議。
