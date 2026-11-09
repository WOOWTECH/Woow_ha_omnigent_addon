# HA Ingress

## 現在怎麼運作（0.1.19 起）

1. HA Supervisor 從 `/api/hassio_ingress/<token>/…` 收到請求，**剝掉前綴**再轉給 add-on 的 8080 port（nginx），並帶上 `X-Ingress-Path` header。
2. nginx（`omnigent/rootfs/etc/nginx/nginx.conf`）把請求轉給 omnigent 的 8000 port，在回應 HTML 的 `<head>` 注入兩樣東西：
   - `<base href="<前綴>/">`
   - `window.__OMNIGENT_BASE_PATH__="<前綴>"`

   前綴取自 `$safe_ingress_path`：它是 `X-Ingress-Path` 經 `map` 驗過格式（`/api/hassio_ingress/<16–128 字元>`）的值，不符合就是空字串。
3. omnigent 0.15 起的前端（上游 `web/src/lib/basePath.ts`）讀 `window.__OMNIGENT_BASE_PATH__`，自己替 REST、SSE、WebSocket、導向、`BrowserRouter basename`、登入導向加上前綴。
4. server 端**不設** base path。HA 用不了上游的 `--base-path`／`OMNIGENT_WEB_BASE_PATH`：ingress 路徑的第一段是 `api`，屬於 `_WEB_UI_API_FALLBACK_PREFIXES` 保留的名稱，`_normalize_base_path` 會 raise `ValueError`，server 一啟動就失敗（`omnigent/server/app.py`，0.15.0–0.21.0 都一樣）。
5. 沒設 base path 時，server 啟動時的 `_rewrite_web_ui_index` 把 index.html 的 `./assets/` 改成根絕對的 `/assets/`，並移除 `<base>`。nginx 再用 `sub_filter` 把 `src="/assets/`、`href="/assets/` 等改寫成 `<前綴>/assets-vX-Y-Z/`，由 `location ~ ^/assets-vX-Y-Z/` 轉回 omnigent 的 `/assets/`。
6. 從 LAN 直接連 8000 port 不經過 nginx，也就沒有注入，前端用根路徑。

## 升版要確認的依賴

`addon-preflight.py` 第 1 段會檢查這三項：

| 依賴 | 為什麼 | 壞掉的樣子 |
|---|---|---|
| 前端 bundle 讀 `__OMNIGENT_BASE_PATH__` | 前綴全靠它。Dockerfile 用 `grep -R -q '__OMNIGENT_BASE_PATH__' …/web-ui/assets/` 當 build 檢查 | build 失敗。上游如果改名，ingress 的 API 與導向全部不帶前綴 |
| index.html 有字面上的 `<head>` | nginx 的注入點是 `sub_filter '<head>'` | 注入不到，症狀同上，但 build 不會失敗 |
| `assets-vX-Y-Z` 等於 add-on 版本 | 版本化的資產網址，避免瀏覽器用到舊版快取的 bundle。這個字串在 `nginx.conf` 出現好幾次 | 改了 add-on 版本卻沒改它，舊快取可能讓延遲載入的頁面黑畫面 |

## 已知的取捨

- **注入了 `<base href>`**：上游刻意移除 `<base>`，因為它會讓 SVG 的 `url(#id)` 與頁內錨點解析到錯的位置。目前沒有看到功能問題，但升版時要留意圖示或錨點的顯示。
- **JS 內的 `"/assets/` 改寫**：0.15 起 JS 改用相對路徑的 import，這幾條 `sub_filter` 多半不再命中，留著也無害。
- **server 自己發出的導向**（magic link、OAuth callback、登出）不知道前綴，要在 HA 上實測。
- **真機驗證紀錄**：
  - 2026-11-08 在 HAOS 18.3 上裝 0.1.19（omnigent 0.20.0），從側欄經 ingress 開啟並登入、LAN 直連 8000 都正常（見 PR #19 的留言）。
  - 2026-11-08 在 HAOS 18.1 上從 0.1.19 升到 0.1.20（omnigent 0.21.0）：ingress 內登入與登出、session 過期後導回 ingress 登入頁再回原頁、Settings 深層連結、`v1/sessions/updates` 的 WebSocket 都正常；圖示顯示正常（見 PR #20 的留言）。

## 0.1.17 以前的做法（已移除）

0.1.17 以前內建 omnigent 0.12.0。當時的做法是在 Dockerfile 用 4 條 sed 直接改 minified bundle，再加上 `ingress-shim.js` 包住 `fetch`、`WebSocket` 等。

這套做法從 0.15 起陸續失效：minifier 換了變數名，登入導向改用 `withBasePath`。0.1.19（#19）整套換成上面的原生做法。之後如果有人又在 Dockerfile 加回 `-e 's#…#…#g'` 形式的 sed 補丁，preflight 第 1 段仍會逐條計算命中次數，0 次就是失效。
