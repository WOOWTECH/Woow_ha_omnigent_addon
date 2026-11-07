---
name: omnigent-upgrade
description: omnigent 升版：把本 add-on 內建的 omnigent（Dockerfile 的 OMNIGENT_VERSION，PyPI 套件）升到新的上游版本，或評估新版能不能升。要升 omnigent、評估上游新版、或升版後使用者要回滾時使用。
---

# omnigent 升版（HA add-on）

本 add-on 用 pip 安裝 PyPI 上的純上游 `omnigent`，版本寫在 `omnigent/Dockerfile` 的 `ARG OMNIGENT_VERSION`。

- **merge 就是發版**：改了 `omnigent/config.yaml` 的 `version` 的 PR，一 merge 就發到 WOOW App Store（見 `AGENTS.md`）。
- **使用者自己更新**：已安裝的使用者在 Supervisor 自己按更新。
- **不能降版**：Store 拒絕降版，所以使用者唯一的回滾路徑，是還原更新前的備份。

## 邊界

- **merge 由覆核者（Eugene）執行。**
  - agent 的工作到「開 PR、等 required checks 全綠、回報結果」為止。
  - PR 標題開頭加 `⛔ 勿代 merge｜待 Eugene 覆核 — `。
  - gh 登入的帳號可能有 admin 權限，技術上做得到下面這些事，但一律留給人：merge、改 ruleset、dispatch workflow、手動發 Release、刪除 Release 或 GHCR tag。
- **本 repo 是 PUBLIC。** commit、PR、檔案只寫公開資訊。
- **語言**：commit message 與 PR 描述用繁體中文（`fix: …`、`feat: …`）。`CHANGELOG.md`、`DOCS.md` 是給使用者看的文件，沿用英文。
- **在 sibling worktree 工作**（`git worktree add ../wt-<名稱> -b <type>/<名稱> origin/main`），主要 checkout 保持乾淨。
- **開工先讀 Store 規則。** 升版比對、升級測試、`upgrade-notes.yaml` 的規則寫在 [Woow_HA_App_Store](https://github.com/WOOWTECH/Woow_HA_App_Store) 的 README。reusable workflow 跟著 `@main` 走，常常改。每次開工先讀最新版的「升版比對與 `upgrade-notes.yaml`」一節，包括其中的「第 2 類 升級測試」與「資料檢查（#138）」。

## 流程

| 階段 | 做什麼 | 完成條件 |
|---|---|---|
| H0 | 盤點現況 | 知道 FROM（目前的 omnigent 版本）、add-on 版本、最新 Release |
| H1 | 上游差異分析，整理「本次決定」 | 使用者確認「本次決定」 |
| H2 | 改版 PR | PR 開出，`addon-preflight.py` 綠燈 |
| H3 | CI 與 HA 實機驗收 | required checks 全綠，`references/ha-acceptance.md` 的清單逐項通過 |
| H4 | merge | 覆核者 merge |
| H5 | 發布驗證 | Release、兩個 arch 的 GHCR tag、Store catalog 都是新版本 |
| H6 | 收尾 | 實際做法和本 skill 不一致的地方，已改回本 skill |

### H0 盤點

```bash
grep -m1 '^version:' omnigent/config.yaml               # add-on 版本
grep -m1 '^ARG OMNIGENT_VERSION=' omnigent/Dockerfile   # omnigent 版本，也就是 FROM
gh release list -R WOOWTECH/Woow_ha_omnigent_addon --limit 3
ls omnigent/upgrade-check 2>/dev/null                   # 有沒有資料檢查 hook
curl -s https://pypi.org/pypi/omnigent/json | python3 -c 'import json,sys; print(json.load(sys.stdin)["info"]["version"])'
```

上游只用 PyPI 上的正式版。`.devYYYYMMDD` 不上 PyPI；`rc` 有上 PyPI，但屬於 pre-release，也不拿來升版。目標版本（TO）由使用者決定。

### H1 上游差異分析

```bash
S=.claude/skills/omnigent-upgrade/scripts
python3 $S/addon-preflight.py <TO>                 # add-on 的耦合點；紅燈＝有耦合點不成立
$S/upstream-diff.sh v<FROM> v<TO> <上游 clone>     # 上游這段期間的變動
```

判讀見 `references/upstream-diff.md`。照判讀結果整理成下面的「本次決定」，交給使用者確認後再往下走：

1. **ingress 補丁**：preflight 第 1 段每條補丁命中幾次？命中 0 次就是失效；失效後會不會壞，看 `references/ingress.md` 的「影響」欄。⇒ 決定怎麼重做。
2. **migration**：新增了哪些 revision？有沒有改資料（`UPDATE`／`DELETE`）？⇒ 決定 CHANGELOG 怎麼寫回滾、`upgrade-check` 要驗哪些資料。
3. **feature flag**：`config.yaml` 預設的清單是否還都認得？新版預設開啟哪些？⇒ 決定 options 預設值與註解要不要改。
4. **CLI／API／env**：preflight 第 2–4 段有沒有 FAIL？⇒ 決定 s6 腳本要不要改。
5. **依賴與 build**：Requires-Dist 新增了什麼？有沒有缺 musllinux wheel、要從 source build 的套件？⇒ 決定 Dockerfile 的 build-deps。
6. **host 相容**：協定 major 有沒有變？tunnel 程式有沒有改？⇒ 決定 `DOCS.md` 要 runner 裝哪一版、要不要用舊 host 實連新 server。
7. **回滾**：使用者怎麼退回？一律是「還原更新前的備份」。把 `alembic_version` 改回舊值的 DB 層回滾，遇到沒有 `IF NOT EXISTS` 的 `add_column`，之後再升級會撞錯，所以不列為選項。

### H2 改版 PR

- **版本字串**：用 `git grep -nF '<FROM>'` 找出所有寫死 omnigent 版本的地方，逐一判斷要不要改。範圍包括 Dockerfile 的 ARG、註解、LABEL，`config.yaml` 的 description 與 feature 註解，`README.md`，`DOCS.md` 裡給 runner 的安裝版本，以及 `init-addon-config` 的註解。add-on 的 `version` 升一版。
- **ingress 補丁**：照「本次決定」重做，見 `references/ingress.md`。改完要達到兩件事：
  - preflight 第 1 段全部綠燈。
  - Dockerfile 的 build 檢查改成逐條確認命中，任何一條沒命中就讓 build 失敗。
- **資料檢查 hook**：在 `omnigent/upgrade-check/` 補上或更新 `seed`／`verify`，見 `references/release.md` 的「資料檢查」。
- **CHANGELOG**（英文）：寫明 omnigent 的新版本、使用者看得到的變化，以及三件事：更新前先做備份、不能降版、回滾就是還原備份。
- **`upgrade-notes.yaml`**：升版比對的報告要求時才建立，`change`、`key` 照報告複製。
- **PR 描述**：寫入「本次決定」、preflight 改前與改後的結果、HA 驗收結果，以及沒驗到的地方。

### H3 CI 與 HA 實機驗收

- **required checks**：名稱以 branch rules 為準，用下面的指令查：`gh api repos/WOOWTECH/Woow_ha_omnigent_addon/rules/branches/main --jq '.[]|select(.type=="required_status_checks")|.parameters.required_status_checks[].context'`。
  - 一輪約 7 分鐘，排隊時會超過 15 分鐘。
  - 要重跑一律用「Re-run all jobs」，原因見 `AGENTS.md`。
- **CI 驗不到的部分**：CI 只驗 amd64 能不能啟動；有 `verify` hook 時才會一起驗資料。ingress 的登入導向、aarch64 的執行期、備份還原，都要在 HA 上照 `references/ha-acceptance.md` 驗。

### H4 merge

由覆核者 merge。agent 回報 PR 連結、checks 結果、HA 驗收結果，然後停下。

### H5 發布驗證

照 `references/release.md` 的「發布之後」確認：Release、兩個 arch 的 GHCR tag、Store 的 catalog 都已經是新版本。

### H6 收尾

實際做法和本 skill 不一致的地方，回頭改本 skill；可以放在同一張 PR，也可以接著開一張。
