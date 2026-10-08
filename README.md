# ETF 投資管理系統

台股與美股 ETF 的報價、排行、殖利率、價格走勢、回測、自選清單與投資組合管理系統。後端使用 FastAPI，前端為 Jinja2 + Tailwind CSS + Chart.js，資料庫支援 TiDB/MySQL 與本機 SQLite。

## 專案結構

```text
main.py                         FastAPI 進入點、啟動週期與健康檢查
application_config.py           環境變數、路徑與應用程式設定
authentication.py               JWT、Google OAuth 與密碼驗證
database.py                     Schema、migration 與資料庫連線管理
database_queries.py             跨功能共用的查詢片段
etf_market_data.py              ETF 商品清單、行情與配息來源整合
market_data_scheduler.py        行情同步、缺口修復與定時工作
memory_cache.py                 執行緒安全的應用程式快取
request_models.py               API 的 Pydantic 請求模型
serialization_utils.py          型別轉換與 JSON 回應工具
routes/                         HTTP 頁面與 API 路由
services/                       回測、評分、匯率、歷史價與通知服務
templates/                      依用途命名的 Jinja2 頁面與共用版型
static/                         CSS、全站 JavaScript、圖示與第三方前端資產
scripts/                        本機啟動及資料來源診斷工具
deployment/                     Cloudflare 等外部部署資產
tests/                          單元、資料流程與頁面完整性測試
```

專案內自訂檔名統一使用小寫 `snake_case`；頁面模板以 `_page.html` 結尾，
共用版型以 `_layout.html` 結尾。框架或套件要求的標準檔名（例如
`package.json`、`Procfile`、`.env.example`）則維持原名，避免破壞工具鏈。

## 本機啟動

1. 安裝 Python 3.12 以上版本與 Node.js 24 LTS。
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

Windows 也可執行 `scripts/start_windows.bat`。腳本會先切換到專案根目錄；
未設定 TiDB 時會自動使用本機 SQLite。

## 前端建置

部署直接提供版本庫內的壓縮 CSS 與 Chart.js，因此修改 Tailwind 類別後應重新建置：

```bash
npm run build
npm run check
```

## 測試與診斷

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q . -x '(^|/)(node_modules|\.git)/'
python3 scripts/data_source_diagnostics.py
```

測試範圍包含報價來源、價格歷史、分割還原、資料同步狀態、快取、前端格式化行為、公開頁面渲染與靜態資產完整性。
GitHub Actions 會在每次推送與 Pull Request 自動重跑 Python 測試、前端建置及語法檢查。

## 免費公開網站部署（Render）

原本 Railway Web Service 若需要付費，**不必要求訪客付錢**：可將 FastAPI 專案改部署到 Render 的 Free Web Service。根目錄的 [`render.yaml`](render.yaml) 已準備免費 Python 服務（新加坡節點、`/health` 健康檢查），不需更改前端、移除使用者登入或購買主機方案。

1. 在 [Render Dashboard](https://dashboard.render.com/) 登入，選擇 **New → Blueprint**，連接 `Nagi0316/etf-system` 的 GitHub `main` 分支，再選擇此專案的 `render.yaml`。
2. 部署設定提示輸入 `DB_HOST`、`DB_USER`、`DB_PASSWORD` 時，請使用**原本 Railway 連接的 TiDB/MySQL 同一套資料庫**。`DB_NAME` 預設 `etf_tracker`，若原本不同請自行修改；`JWT_SECRET` 自動產生新的安全值。絕不能把金鑰或密碼提交至 GitHub。**免費 Render 的 SQLite 無法持久保存資料**，網站因此會在沒有遠端資料庫設定時拒絕啟動，而不是讓用戶資料日後無聲消失。
3. 如需使用 Google 登入，填入 `GOOGLE_CLIENT_ID` 與 `GOOGLE_CLIENT_SECRET`，並在 Google Cloud OAuth 用戶端的「已授權的重新導向 URI」新增 `https://<實際服務網址>.onrender.com/api/auth/google/callback`。APP_URL 和回呼 URL 未另外設定時會由 Render 網址自動推導；有自訂網域時應分別設定正確的 `APP_URL` 和 `GOOGLE_REDIRECT_URI`。
4. 部署完成後檢查網站首頁、`/health`、ETF 查詢及登入流程。確認新站讀取的是既有資料庫，然後才修改對外網址並關閉 Railway 計費服務；勿先刪除 Railway 上的環境變數或資料庫。

**免費方案限制：** Render Free Web Service 閒置 15 分鐘會休眠，下次訪客開啟可能約需一分鐘喚醒；每個工作區每月包含 750 免費執行小時，另有流量與建置額度。休眠時站內 APScheduler 不會繼續執行定時行情同步，因此價格可能暫時較舊；需要可靠的全天候背景同步時，應另外採用可持續執行的排程架構。TiDB/MySQL 本身是否免費由其服務方案決定；免費主機不等於外部服務永遠無費用。詳情見 [Render 免費方案說明](https://render.com/docs/free)。

## 資料正確性原則

- 台股報價優先使用證交所/櫃買中心公開資料，美股與歷史價設有多來源備援。
- 價格資料保留交易日與來源；健康檢查會回報覆蓋率與最後成功時間。
- 殖利率區分 `confirmed`、`estimated`、`not_applicable` 與 `unknown`，不把缺少資料誤當成 0%。
- 配息缺口採持久化公平佇列分批補齊；來源失敗只記錄嘗試狀態，不會寫入猜測值，也不會讓同一批失敗標的阻塞後續代碼。
- 盤中全市場同步只執行有硬逾時的批量來源；較慢的逐檔備援由詳情頁或收盤後作業處理，避免排程重疊拖慢網站。
- 已下市或已確認無效標的不參與排行與日常同步。

本系統用於資訊整合與學術展示，不構成投資建議。
