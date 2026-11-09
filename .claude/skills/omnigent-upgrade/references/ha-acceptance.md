# H3：HA 實機驗收

CI 只驗 amd64 版能不能啟動。下面這些要在一台真的 HAOS 上驗；能找到 aarch64 的機器，就兩個 arch 都驗。

## 測試機

一台可以清掉的 HAOS 就好，例如用官方 qcow2（`haos_ova-<版本>.qcow2.xz`）開一台 VM，2 vCPU、6 GiB 記憶體、40 GiB 磁碟。要在 VM 裡 build add-on 的話，記憶體給 8 GiB。這裡只寫 HAOS 本身的注意事項；測試環境的平台細節不寫進本 repo（見 SKILL.md 的公開 repo 規則）。

- **HA 網頁在 VM 的 80 port。** HAOS 18.x 連 8123 會被 307 轉到同一個主機的 80 port。從外面轉 port 進 VM 時要轉 80，不能只轉 8123。
  - 例外：開機後 Supervisor 還在 landing page 階段時方向相反，80 會轉到 8123。等 `/api/onboarding` 在 80 回 200 再開始操作。
- **要轉進 VM 的 port**：HA 網頁的 80、add-on 的 8000、SSH 的 22。
  - 有些轉 port 的工具，只要一條連線失敗就整個結束。例如 add-on 停止時瀏覽器還在重連 8000，連 HA 網頁也一起斷掉；VM 剛開機、網路還沒好時也會這樣。每個 port 各開一條，並用「斷了就重開」的迴圈包起來。
- **HAOS 磁碟不要放在主機的系統碟上。** 系統碟滿了，同一台主機上的其他服務也會受影響。
- **Claude Code 的 auto mode 會擋下在共用環境開、刪 VM。** 把設定寫好、能 dry run 就先 dry run，再請使用者自己用 `! <指令>` 執行；刪除時也一樣。
- 用完整個刪掉，磁碟一起回收。

## 裝法

- **測已發布的版本**：在 add-on 商店加入本 repo 的網址，直接安裝。
- **測 PR 版本**：PR 的 image 不會推到 GHCR。要把 `omnigent/` 以 local add-on 的方式放進 HAOS 的 `/addons`，讓 Supervisor 自己 build。
  - 只在測試機上的那份做改動：`config.yaml` 拿掉 `image`，Supervisor 才會在本機 build（有 `image` 時會去拉 image）。
  - amd64：Dockerfile 的 `ARG BUILD_FROM` 預設值就是 CI 用的 amd64 base。新版 Supervisor 沒有 `build.yaml` 時會用這個預設值，不用另外設定。
  - aarch64：Dockerfile 的預設值是 amd64 的 base，所以要加一個 `build.yaml`，把 `build_from` 設成 CI matrix 裡 aarch64 的 base（見 `publish-omnigent-addon-images.yml`）。`build.yaml` 已被 Supervisor 標為 deprecated，會印 warning，屬正常。
- **要測「從 FROM 升級到 TO」時，FROM 和 TO 都要用 local add-on。** local add-on 的 slug 是 `local_woow-omnigent`，商店版是 `<repo hash>_woow-omnigent`。兩者是不同的 add-on，不共用 `/data`，也不能互相更新。
- **HA 2026.11 起 add-on 改稱 app。** 官方 SSH add-on 裡，`/addons` 變成 `/local_apps`，`/addon_configs` 變成 `/app_configs`；CLI 是 `ha apps …`（例：`ha apps logs local_woow-omnigent`）。
- **不開瀏覽器也能全部用 API 做完**（2026-11-08 的做法）：
  - onboarding：依序呼叫 `/api/onboarding/users`、`/auth/token`、`/api/onboarding/{core_config,analytics,integration}`，再用 websocket 的 `auth/long_lived_access_token` 換一個 token。
  - Supervisor：經由 HA websocket 的 `{"type": "supervisor/api", "endpoint": …, "method": …}` 呼叫。安裝是 `POST /store/addons/<slug>/install`，更新是 `POST /store/addons/<slug>/update`，改原始碼後先 `POST /store/reload`。
  - 放原始碼：裝官方 SSH add-on（`core_ssh`，設 `authorized_keys` 與 `network: {"22/tcp": 22}`），再用 `tar` 經 ssh 解到 `/local_apps/woow-omnigent`。
  - **用 API 安裝時 `ingress_panel` 預設是 false**，側欄不會出現 Omnigent。要 `POST /addons/<slug>/options {"ingress_panel": true}`。從 UI 安裝不受影響。
  - 瀏覽器項目（#2–#5、#9）用 Playwright 跑 headless Chromium：在 HA 登入頁登入，點 `ha-sidebar` 裡的「Omnigent」，再找 URL 含 `/api/hassio_ingress/` 的 iframe 操作。omnigent 在 HTTP 下的 session cookie 叫 `ap_session`，#4 刪它即可。

## 準備（測升級時）

1. 放入 FROM 的原始碼（目前已發布版本的 tag），build 並啟動，設定 `admin_password`。
2. 在 FROM 上製造資料。不需要 runner 的最簡單做法：在 Settings → Members 按 Invite member，建一個名稱好認的成員當記號。有 runner 的話，再建一個 session。
   - 也可以直接跑 PR 裡的 `omnigent/upgrade-check/seed`：`ADDON_URL` 指向轉進來的 8000，`DATA_DIR_DATA` 放一份含 `admin_username`／`admin_password` 的 `options.json`。升級後用同樣的環境變數跑 `verify`，就是 #6。
   - 為了 #12，升級後再建一個成員：回滾之後它應該不見。
3. **在 FROM 上先跑一遍下面的清單**，當作基準。FROM 就已經失敗的項目，標成既有問題，不算新版回歸。
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
| 6 | 舊資料 | FROM 建的記號成員、session、帳號都還在 |
| 7 | runner | FROM 版本的 host 還連得上，能開 session、收到回覆 |
| 8 | terminal | session 的 terminal 打得開，WebSocket 連得上 |
| 9 | LAN 直連 | `http://<HAOS>:8000` 直接打開、登入正常 |
| 10 | 乾淨關機 | 按「停止」後，日誌依序出現 `received smart shutdown request` 與 `database system is shut down` |
| 11 | 備份還原 | 用 TO 做一份部分備份（只含本 add-on），再還原，add-on 起得來，記號成員還在 |
| 12 | 回滾 | 還原「準備」階段的 FROM 備份，FROM 版本起得來，資料回到當時的內容。FROM 是 0.1.17 以前的版本時，備份沒有 `pg_wal`，會起不來（見 `release.md` 最後一節） |

結果寫進 PR 描述。沒驗的項目要明寫沒驗與原因。

驗收紀錄：

- 2026-11-08 對 0.1.19（全新安裝，不是升級）驗過 #2、#9、#10、#11，見 PR #19 的留言。
- 2026-11-08 對 0.1.19 → 0.1.20 跑完整份清單（HAOS 18.1，amd64）：#7 只驗了 0.20.0 的 host 連得上，#8 沒驗，其餘通過。見 PR #20 的留言。
