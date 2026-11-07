# HA Ingress 與 web-ui 補丁

## 現在怎麼運作

1. HA Supervisor 從 `/api/hassio_ingress/<token>/…` 收到請求，**剝掉前綴**再轉給 add-on 的 8080 port（nginx），並帶上 `X-Ingress-Path` header。
2. nginx（`omnigent/rootfs/etc/nginx/nginx.conf`）把請求轉給 omnigent 的 8000 port，回應的 HTML 在 `<head>` 注入三樣東西：
   - `<base href="<前綴>/">`
   - `window.__INGRESS_PATH__`
   - `/_ingress-shim.js`

   另外用 `sub_filter` 把 HTML 裡的 `src="/assets/`、`href="/assets/` 改寫成 `<前綴>/assets-v0-1-13/`。
3. `ingress-shim.js` 做兩件事：
   - 先用 `history.replaceState` 把網址列的前綴剝掉，讓 SPA 的 router 看到 `/login` 這種路徑。
   - 再包住 `fetch`、`XMLHttpRequest.open`、`WebSocket`、`Location.assign/replace`，送出時把前綴加回去。
   - 沒包的有兩個：`history.pushState/replaceState` 刻意不包，因為 SPA 用的就是已剝掉前綴的路徑；`location.href = …` 包不住，Chrome 擋 setter。
   - 所以登入導向要靠 Dockerfile 的 sed 補丁。
4. 從 LAN 直接連 8000 port 時沒有 `window.__INGRESS_PATH__`，以上全部不作用。

## Dockerfile 的 4 條 sed 補丁

補丁直接改 wheel 裡 minified 的 `web-ui/assets/*.js`，字串必須逐字相符。上游每次 build，變數名稱都可能換掉。

| # | 原本的作用 | 命中的版本 | 失效的影響 |
|---|---|---|---|
| P1 | 放寬「現在在不在 /login」的判斷 | 0.12–0.17；0.18 起失效（minifier 換了變數名） | 無害：shim 已先剝掉前綴，原本的判斷就對了 |
| P2 | `return_to` 帶上前綴 | 0.12 起，至少到 0.21 都還命中 | — |
| P3 | 401 後 `location.href` 組登入網址時加前綴 | 只有 0.12–0.14；0.15 起失效 | **有害**：session 過期時，iframe 會被導到 HA 根目錄的 `/login?return_to=…`，跳出 ingress（靜態推論，尚未在 HA 上實測） |
| P4 | Vite modulepreload helper 加前綴 | 只有 0.12–0.14；0.15 起失效 | 無害：0.15 起 JS 改用相對路徑，helper 變成 `new URL(e, t).href` |

0.15 起的登入導向改寫成 `location.href = \`${withBasePath(loginUrl)}?return_to=…\``，前綴來自 `window.__OMNIGENT_BASE_PATH__`（上游 `web/src/lib/basePath.ts`、`identity.ts`）。

連 0.12.0 本身，登出（`location.href = '/auth/logout'`）也沒有補丁。所以 HA 驗收時，要先在 FROM 上跑一次當基準。

**Dockerfile 的 build 檢查有兩個盲點。** 檢查指令是 `grep -R -l 'window.__INGRESS_PATH__' … | head -3`：

- 在 base image 的 `pipefail` 下，一條都沒命中才會失敗；只命中一條也算通過。
- P1 的替換字串裡沒有 `window.__INGRESS_PATH__`，所以這個 grep 永遠看不到 P1。

升版時把檢查改成逐條確認命中。沒有共同標記的補丁（例如 P1），用它自己替換後的字串來驗。

## 上游的原生子路徑（0.15 起）不能直接設定

- server 端有 `--base-path` 與 `OMNIGENT_WEB_BASE_PATH`。設定之後：
  - server 會在 index.html 注入 `window.__OMNIGENT_BASE_PATH__`；
  - `BasePathMiddleware` 遇到請求帶著前綴就剝掉，沒帶前綴（代理已經剝掉）的請求照常處理。
- **HA 用不了這個設定。** ingress 路徑的第一段是 `api`，屬於 `_WEB_UI_API_FALLBACK_PREFIXES` 保留的名稱。`_normalize_base_path` 遇到它會 raise `ValueError`，server 一啟動就失敗（`omnigent/server/app.py`，0.15.0–0.21.0 都一樣）。
- 沒有設定 base path 時，server 啟動時的 `_rewrite_web_ui_index` 會把 index.html 的 `./assets/` 改成根絕對的 `/assets/`，並移除 `<base>`。所以 nginx 收到的 HTML 仍是根絕對路徑。

## 重做的兩個方向

兩個方向都要在 HA 上照 `ha-acceptance.md` 驗過才能發版。

**A. 照新版 bundle 重寫 sed**

- 找出新版裡對應 P3 的片段，例如 `window.location.href=\`${i(e)}?return_to=${t}\``，重寫 sed；P1、P4 拿掉。
- 改動最小，但每次升版都要重找。
- 新版裡其他十幾處 `location.href`（登出、OAuth connect 等）同樣不會帶前綴，這個方向補不到。

**B. 改用上游的子路徑機制，由 nginx 逐請求注入**

- nginx 在 `<head>` 的最前面注入 `window.__OMNIGENT_BASE_PATH__="<前綴>"`。前綴取自 `X-Ingress-Path`，沿用現有的 `$safe_ingress_path`。
- server 端不設 base path。Supervisor 本來就會剝掉前綴，和上游支援的「轉發時剝掉前綴」相同。
- SPA 的 REST、SSE、WebSocket、導向、`BrowserRouter basename` 都會自己加上前綴，可以拿掉 4 條 sed，以及 shim 的大部分功能。
- 要一起處理的地方：
  - shim 的 `replaceState` 剝前綴要拿掉，否則會和 `basename` 衝突。
  - P2 要拿掉，否則 `return_to` 會帶兩次前綴。
  - 注入的 `<base href>` 要拿掉：上游刻意移除 `<base>`，因為它會讓 SVG 的 `url(#id)` 解析錯誤。
  - HTML 的 `src="/assets/`、`href="/assets/` 這幾條 `sub_filter` 要保留：server 送出的 HTML 是根絕對路徑，拿掉之後 `<script src="/assets/…">` 會打到 HA 根目錄。
  - 針對 JS 字串的 `sub_filter` 可以拿掉。
- 風險：
  - `window.__OMNIGENT_BASE_PATH__` 是上游的內部介面，不是對外承諾。
  - server 自己發的導向（magic link、OAuth callback、登出）不知道前綴，行為要實測。
