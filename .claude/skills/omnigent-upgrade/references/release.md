# 發版鏈、資料檢查、發布之後

規則的權威來源是 `AGENTS.md`，以及 Woow_HA_App_Store 的 README。這裡只寫 omnigent 升版會用到的部分。

## PR 上的檢查

- `publish-omnigent-addon-images.yml` 會：
  - build amd64 與 aarch64，PR 的 image 不推到 GHCR；
  - 把 amd64 的 image 交給 `Upgrade comparison`。
- `Upgrade comparison / compare` 做四類比對：
  1. **設定相容性**：比對 `config.yaml` 的 schema。破壞性變更要寫進 `upgrade-notes.yaml`，才會放行。
  2. **升級測試**：在 mock Supervisor 下，先跑最新 Release 的 image，再用同一份 `/data` 跑 PR 的 image。`/health` 要有回應，容器要撐過 60 秒。有 hook 時一併檢查資料，見下一節。
  3. **回歸比對**：有 `smoke` hook 時，新舊版各跑一次；舊版通過、新版失敗的項目會被判為回歸。
  4. **漏洞比對**：新 image 才有、而且有修正版本的 Critical／High 漏洞會被擋下。omnigent 升版會連帶改變沒有 lock 的 pip 依賴，很容易碰到。被擋時，要在 `upgrade-notes.yaml` 的 `accepted_vulnerabilities` 宣告（`id`、`package`、`reason`），或者升級那個套件。
- `test-admin-password.yml` 是離線測試，不是 required check，但失敗就要修。

## 資料檢查（`omnigent/upgrade-check/`）

沒有 hook 時，升級測試只驗啟動（報告會寫「只檢查啟動，沒有檢查資料」）。多數升版都有 migration，跨多版時一定有，有沒有以 `upstream-diff.sh` 的 alembic 段為準。有 migration 的升版 PR，要有 `seed` 與 `verify`。

- **執行環境**：hook 在 mock Supervisor 的容器裡跑（`python:3.12-alpine`）。
  - 只能用 python3 標準函式庫或 busybox sh，沒有 bash，也沒有 psql。
  - 透過 HTTP 連 add-on，網址用 `$ADDON_URL`，就是 8000 port 的 web UI。
  - admin 帳密從 `$DATA_DIR_DATA/options.json` 讀；`admin_password` 由框架產生。
- **檔案規則**：
  - 要可執行（`git update-index --chmod=+x`），行尾 LF。
  - 結束前不能留下背景程序。
  - 報告寫到 `$REPORT_DIR/<hook>.json`。
  - 完整介面見 Store README 的「資料檢查（#138）」。
- **建議的內容**：
  - `seed`（跑在舊版上）：用 admin 登入，透過 API 建立會被這次 migration 碰到的資料（例如 session、agent、使用者偏好），把 id 寫進 `$HOOK_STATE_DIR`。
  - `verify`（跑在新版上）：用同一組帳密登入，讀回這些資料，確認內容相同。
  - `smoke`（選用，新舊版各跑一次，只讀）：登入、列出 session、打開 `/v1/info`。在舊版通過、在新版失敗的項目，會被判為回歸。
- **目前的 hook**（0.1.20 起）：邏輯都在 `omnigent/upgrade-check/omnigent_upgrade.py`，`seed`／`verify` 只是呼叫它。
  - `seed` 建一個暫停中的 scheduled task（不需要 runner），以及一個經由 invite → register 建立的記號成員。
  - `verify` 讀回來比對欄位，並用原密碼登入 admin 與記號成員。
  - 新版的 migration 動到別的資料表時，在這裡加上對應的資料。
- **先在本機測 hook**（本機沒有 docker 也可以）：
  - 開兩個 venv，分別裝 FROM 與 TO 的 omnigent。用 accounts auth＋sqlite 起 server，啟動參數可參考 Hermes plugin 的 `tests/server-contract/run.py`。
  - 舊版跑 `seed`，停掉，新版用同一份資料跑 `verify`。環境變數要給 `ADDON_URL`、`DATA_DIR_DATA`（放一份 `options.json`）、`HOOK_STATE_DIR`、`REPORT_DIR`。
  - 再做一次反向測試：`verify` 之前刪掉 `seed` 建的資料，確認 `verify` 會失敗。
  - sqlite 與 Postgres 的 migration 行為不同，真正的升級仍以 CI 為準。

## merge 之後的發布鏈

1. push 到 main 之後，`publish-omnigent-addon-images.yml` 會：
   - build 兩個 arch，推到 `ghcr.io/woowtech/woow-ha-omnigent-{amd64,aarch64}:<add-on 版本>`。tag 不帶 `v`；已存在的版本不會覆寫。
   - 發 GitHub Release `v<add-on 版本>`。
   - Release 會出現在 merge 之後約 7–8 分鐘。
2. `woow-addon-sync.yml` 把 commit、雜湊、image digest 寫進 `woow-addon-sync` 分支，再通知 Store。
3. Store 驗過後同步到它的 repo。使用者的 Supervisor 重新整理之後，就會看到更新。

## 發布之後

```bash
V=<add-on 版本>
gh release view v$V -R WOOWTECH/Woow_ha_omnigent_addon --json tagName,publishedAt
for a in amd64 aarch64; do
  T=$(curl -s "https://ghcr.io/token?scope=repository:woowtech/woow-ha-omnigent-$a:pull" | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
  curl -s -o /dev/null -w "$a %{http_code}\n" -H "Authorization: Bearer $T" \
    -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.v2+json' \
    "https://ghcr.io/v2/woowtech/woow-ha-omnigent-$a/manifests/$V"
done
gh api repos/WOOWTECH/Woow_HA_App_Store/contents/.addon-sync/catalog.json --jq .content | base64 -d \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["components"]["woow-omnigent"]["version"])'
```

- Release 存在，兩個 arch 都回 200，catalog 的版本等於 `$V`，才算發布完成。
- catalog 慢於 Release 是正常的：Store 的排程同步可能延遲數小時。
- 超過一天還沒同步，就看 `woow-addon-sync.yml` 最近一次 run 的結果。

## 給使用者的回滾說明

Store 拒絕降版，不能靠發一個較小的版號回滾。使用者的路徑只有一條：還原更新前的 add-on 備份。Supervisor 還原時，會裝回備份當時的 add-on 版本。所以 CHANGELOG 要寫明「更新前先做備份」。

- 0.1.18 起，備份包含 `pg_wal`。2026-11-08 在真機 HAOS 上驗過：add-on 停止時 s6 先停 omnigent server 再停 Postgres，Postgres 是乾淨關機，部分備份還原後資料完整。
- 0.1.17 以前的備份沒有 `pg_wal`：還原後 Postgres 會停在 `PANIC: could not locate a valid checkpoint record`，目前的版本沒有補救機制。需要時可以參考已關閉的 PR #16（`pg-wal-rescue`：乾淨關機的資料用 `pg_resetwal` 重建 WAL）。
