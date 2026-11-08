# H3：HA 實機驗收

CI 只驗 amd64 版能不能啟動。下面這些要在一台真的 HAOS 上驗；能找到 aarch64 的機器，就兩個 arch 都驗。

## 測試機

一台可以清掉的 HAOS 就好，例如用官方 qcow2（`haos_ova-<版本>.qcow2.xz`）開一台 VM，2 vCPU、6 GiB 記憶體、40 GiB 磁碟。2026-11-08 用 KubeVirt 開過一台，踩到這些坑：

- **HA 網頁在 VM 的 80 port。** HAOS 18.x 連 8123 會被 307 轉到同一個主機的 80 port。做 port-forward 時要轉 80，不能只轉 8123。
- **CDI 匯入時磁碟會佔三份。** target、prime、scratch 三顆 PVC 各約 1.06 倍的磁碟大小，ResourceQuota 的 `requests.storage` 要留足。40Gi 的磁碟要約 130Gi。
- **`kubectl port-forward` 一次轉多個 port 時，任一個 port 連線失敗，整個 port-forward 會結束。** 例如 add-on 停止時，瀏覽器還在重連 8000，連 HA 網頁也一起斷掉。每個 port 各開一個 port-forward。
- **不要把 HAOS 磁碟放在節點系統碟上**（例如 `local-path`）。系統碟滿了，整台節點的 pod 都會被趕走。
- 用完整個刪掉，磁碟一起回收。

## 裝法

- **測已發布的版本**：在 add-on 商店加入本 repo 的網址，直接安裝。
- **測 PR 版本**：PR 的 image 不會推到 GHCR。要把 `omnigent/` 以 local add-on 的方式放進 HAOS 的 `/addons`，讓 Supervisor 自己 build。
  - 只在測試機上的那份做改動：`config.yaml` 拿掉 `image`，Supervisor 才會在本機 build（有 `image` 時會去拉 image）。
  - amd64：Dockerfile 的 `ARG BUILD_FROM` 預設值就是 CI 用的 amd64 base。新版 Supervisor 沒有 `build.yaml` 時會用這個預設值，不用另外設定。
  - aarch64：Dockerfile 的預設值是 amd64 的 base，所以要加一個 `build.yaml`，把 `build_from` 設成 CI matrix 裡 aarch64 的 base（見 `publish-omnigent-addon-images.yml`）。`build.yaml` 已被 Supervisor 標為 deprecated，會印 warning，屬正常。
- **要測「從 FROM 升級到 TO」時，FROM 和 TO 都要用 local add-on。** local add-on 的 slug 是 `local_woow-omnigent`，商店版是 `<repo hash>_woow-omnigent`。兩者是不同的 add-on，不共用 `/data`，也不能互相更新。

## 準備（測升級時）

1. 放入 FROM 的原始碼（目前已發布版本的 tag），build 並啟動，設定 `admin_password`。
2. 在 FROM 上製造資料。不需要 runner 的最簡單做法：在 Settings → Members 按 Invite member，建一個名稱好認的成員當記號。有 runner 的話，再建一個 session。
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

2026-11-08 對 0.1.19（全新安裝，不是升級）驗過 #2、#9、#10、#11，見 PR #19 的留言。
