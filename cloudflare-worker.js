/**
 * ETF System — Yahoo Finance Proxy Worker
 *
 * 部署步驟：
 *   1. 登入 https://dash.cloudflare.com → Workers & Pages → Create
 *   2. 貼入此程式碼，Worker 名稱任意（如 etf-yahoo-proxy）
 *   3. Settings → Variables → 新增 Secret：  SECRET = <任意隨機字串>
 *   4. 部署後取得 URL：https://etf-yahoo-proxy.<your>.workers.dev
 *   5. 在 Railway 環境變數設定：
 *        CF_PROXY_URL    = https://etf-yahoo-proxy.<your>.workers.dev
 *        CF_PROXY_SECRET = <與 Worker SECRET 相同的字串>
 *
 * 呼叫格式（Python 端自動組裝，無需手動呼叫）：
 *   GET https://<worker-url>?u=<encoded-yahoo-url>
 *   Header: X-Proxy-Secret: <SECRET>   ← Secret 改從 Header 傳，不再出現在 URL
 */

export default {
  async fetch(request, env) {
    // CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        headers: {
          "Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Methods": "GET, OPTIONS",
          "Access-Control-Allow-Headers": "*",
        },
      });
    }

    if (request.method !== "GET") {
      return new Response(JSON.stringify({ error: "Method not allowed" }), {
        status: 405,
        headers: { "Content-Type": "application/json" },
      });
    }

    const reqUrl = new URL(request.url);

    // ── 驗證 Secret（從 Header 讀取，避免 Secret 出現在 URL/log 中）──
    const secret = request.headers.get("X-Proxy-Secret");
    if (!env.SECRET || secret !== env.SECRET) {
      return new Response(JSON.stringify({ error: "Unauthorized" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      });
    }

    // ── 取得目標 URL ──
    const target = reqUrl.searchParams.get("u");
    if (!target) {
      return new Response(JSON.stringify({ error: "Missing u parameter" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    // ── 白名單：只允許 Yahoo Finance ──
    const ALLOWED = [
      "https://query1.finance.yahoo.com",
      "https://query2.finance.yahoo.com",
      "https://finance.yahoo.com",
    ];
    if (!ALLOWED.some((h) => target.startsWith(h))) {
      return new Response(JSON.stringify({ error: "Host not allowed" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      });
    }

    // ── 轉發請求到 Yahoo Finance ──
    try {
      const resp = await fetch(target, {
        method: "GET",
        headers: {
          Accept: "application/json, text/plain, */*",
          "User-Agent":
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
          "Accept-Encoding": "gzip, deflate, br",
          Referer: "https://finance.yahoo.com/",
          Origin: "https://finance.yahoo.com",
          DNT: "1",
        },
        redirect: "follow",
        // Cloudflare Worker 預設 30s timeout，足夠處理大型回應
      });

      const body = await resp.arrayBuffer();

      return new Response(body, {
        status: resp.status,
        headers: {
          "Content-Type":
            resp.headers.get("Content-Type") || "application/json",
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "no-store",
          "X-Proxy-Status": String(resp.status),
        },
      });
    } catch (err) {
      return new Response(
        JSON.stringify({ error: `Proxy fetch failed: ${err.message}` }),
        {
          status: 502,
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        }
      );
    }
  },
};
