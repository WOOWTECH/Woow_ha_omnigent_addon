# H3：HA 實機驗收

CI 只驗 amd64 版能不能啟動。下面這些要在一台真的 HAOS 上驗；能找到 aarch64 的機器，就兩個 arch 都驗。

## 裝法：FROM 和 TO 都用 local add-on

PR 的 image 不會推到 GHCR。在 HAOS 上測 PR 版本，要把 `omnigent/` 以 local add-on 的方式放進 HAOS 的 `/addons`，讓 Supervisor 自己 build。

- **FROM 和 TO 都要用 local add-on。** local add-on 的 slug 是 `local_woow-omnigent`，商店版是 `<repo hash>_woow-omnigent`。兩者是不同的 add-on，不共用 `/data`，也不能互相更新。
- 只在測試機上的那份做改動：
  - `config.yaml` 拿掉 `image`，Supervisor 才會在本機 build（有 `image` 時會去拉 image）。
  - amd64：Dockerfile 的 `ARG BUILD_FROM` 預設值就是 CI 用的 amd64 base。新版 Supervisor 沒有 `build.yaml` 時會用這個預設值，不用另外設定。
  - aarch64：Dockerfile 的預設值是 amd64 的 base，所以要加一個 `build.yaml`，把 `build_from` 設成 CI matrix 裡 aarch64 的 base（見 `publish-omnigent-addon-images.yml`）。`build.yaml` 已被 Supervisor 標為 deprecated，會印 warning，屬正常。舊版 Supervisor 沒有 `build.yaml` 時會改用自己的預設 base，同樣要加。

## 準備

1. 放入 FROM 的原始碼（目前已發布版本的 tag），build 並啟動，設定 `admin_password`。
2. 在 FROM 上製造資料：
   - 登入 web UI，建一個 session。
   - 從另一台機器用 FROM 版本的 `omnigent host --server http://<HAOS>:8000` 註冊一個 runner。
3. **在 FROM 上先跑一遍下面的清單**，當作基準。FROM 就已經失敗的項目，標成既有問題，不算新版回歸。例如 0.12.0 的登出本來就沒有 ingress 補丁。
4. 做一份 add-on 備份。

然後把 `/addons` 裡的原始碼換成 TO（PR 分支），在 Supervisor 按 Rebuild 或 Update。

## 清單

| # | 驗什麼 | 通過條件 |
|---|---|---|
| 1 | 升級 | 換成 TO 後，log 沒有 error，`/health` 有回應，migration 跑完 |
| 2 | 側欄開啟 | 從 HA 側欄打開 Omnigent，畫面正常，瀏覽器 console 沒有 404 |
| 3 | 登入、登出 | 能登入；登出後回到 ingress 內的登入頁，沒有跳到 HA 根目錄 |
| 4 | session 過期 | 刪掉 cookie 或等 session 過期後操作，被導到 ingress 內的登入頁，登入後回到原頁面 |
| 5 | 延遲載入的頁面 | Settings 下的 Members、Policies、Sharing 都打得開（0.1.15 修過黑畫面） |
| 6 | 舊資料 | FROM 建的 session、帳號都還在 |
| 7 | runner | FROM 版本的 host 還連得上，能開 session、收到回覆 |
| 8 | terminal | session 的 terminal 打得開，WebSocket 連得上 |
| 9 | LAN 直連 | `http://<HAOS>:8000` 直接打開、登入正常 |
| 10 | 備份還原 | 用 TO 做一份備份，再還原，add-on 起得來、資料都在 |
| 11 | 回滾 | 還原「準備」階段的 FROM 備份，FROM 版本起得來、資料回到當時的內容。FROM 是 0.1.17 以前的版本時，備份沒有 `pg_wal`，要先更新到 0.1.18 以上才救得回來（見 `release.md` 最後一節） |

結果寫進 PR 描述。沒驗的項目要明寫沒驗與原因。
