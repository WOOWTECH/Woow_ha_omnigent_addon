# H1：上游差異分析

## 上游來源

- **原始碼**：<https://github.com/omnigent-ai/omnigent>。
  - 正式版有 tag `vX.Y.Z`，同時發布到 PyPI `omnigent`。
  - nightly 的 `vX.Y.Z.devYYYYMMDD` 不上 PyPI。
  - `rc` 有上 PyPI，但屬於 pre-release：pip 預設不裝，`info.version` 也不會是它。不拿來升版。
- **clone**：
  - 沒有的話，建一個不 checkout 的：`git clone --no-checkout --filter=blob:none https://github.com/omnigent-ai/omnigent.git <dir>`。
  - 這種 clone 只用 `show`／`ls-tree`／`diff`／`grep`／`archive`。
  - 第一次跑 `upstream-diff.sh` 會按需下載 blob，要幾分鐘。
- **只比兩個 tag 的 tree**：
  - minor release 各自在自己的 `release/vX.Y.0` 分支上，彼此不是祖孫關係，`FROM..TO` 的 log 會混進 main 上的 commit。
  - patch release 接在同一條分支上（例如 v0.19.1 在 `release/v0.19.0`），是祖孫關係。
  - 兩種情況都一律用 `git diff FROM TO`。
- **回報上游**：走 GitHub 公開 Issues（英文）。

## 兩支腳本的分工

- **`addon-preflight.py <TO>`** 判定 add-on **實際用到的東西**在 TO 還在不在。
  - 它從 PyPI 下載 FROM 與 TO 的 wheel，核對 sha256；不安裝，只讀 wheel 內容。
  - 有 FAIL 就是紅燈（exit 1）。
  - wheel 會快取在 `~/.cache/omnigent-addon-preflight`。
- **`upstream-diff.sh`** 列出上游這段期間**改了什麼**，交給人判讀。

## 判讀表

| 段落 | 看什麼 | 怎麼影響後面 |
|---|---|---|
| preflight 1：sed 補丁 | 每條在 TO 的 `assets/*.js` 命中幾次 | 0 次＝補丁失效。build 照樣會綠燈，因為 Dockerfile 的檢查只要求至少一條命中。重做方式見 `ingress.md` |
| preflight 1：index.html | wheel 裡的靜態 index.html，資產路徑是絕對（`/assets/`）還是相對（`./assets/`），有沒有 `<base href>` | 0.15 起靜態檔改成相對路徑，但 server 啟動時的 `_rewrite_web_ui_index` 會改回根絕對的 `/assets/…`，並移除 `<base>`。所以 nginx 實際收到的 HTML 仍是根絕對，HTML 的 `sub_filter` 規則仍在作用。JS 內部字串改用相對路徑，針對 JS 的 `sub_filter` 規則因此不再作用，但沒有害處 |
| preflight 2：CLI flag | `svc-omnigent-server` 傳的 flag 在 TO 還在不在 | 不在的話，server 起不來 |
| preflight 3：HTTP API | `init-admin-claim` 用的 `/health`、`/v1/info`、`/auth/setup` | 不在的話，自動建立 admin 會失敗 |
| preflight 4：env | add-on 設定的 `OMNIGENT_*`，在 FROM 有讀、TO 不讀了 | 設定會無聲失效，要改用新的名稱。標成「add-on 自己用的」那幾個，omnigent 本來就不讀 |
| preflight 5：feature flag | `config.yaml` 預設清單裡的名稱在 TO 認不認得；TO 預設開啟哪些 | 不認得的名稱會讓 server 拒絕啟動。<br>0.19 起有 `default_enabled`：預設開啟的 feature 不必列出，列出了也關不掉，關閉要寫 `-name`。<br>`config.yaml` 註解寫的「列出全部已知 feature」要跟著改 |
| preflight 6：requires-python／Requires-Dist | 新增或改 pin 的依賴 | 新的 native 依賴在 Alpine（musl）上可能沒有 wheel，要從 source build，見下方「build」 |
| preflight 6：alembic | 新增幾個 revision；TO 是否只有一個 head；有沒有移除 revision | 新 head 寫進 DB 之後，舊版 image 會拒絕啟動，使用者只能還原備份。有移除 revision 的話，跨多版升級可能接不上，要先查上游的說明 |
| upstream-diff：migration 的資料操作 | `UPDATE`／`DELETE`／`op.execute` | 會改資料或丟資料的 migration，要寫進 CHANGELOG，也要讓 `upgrade-check` 的 `seed` 寫入相關的資料 |
| upstream-diff：不可重跑的 DDL | 沒有 `IF NOT EXISTS` 的 `add_column`／`create_index`，或 drop 之後重建 | 把 `alembic_version` 改回舊值後再升級會撞錯（例：0.12→0.19.1 的 `hh2c3d4e5f6a` 直接 `op.add_column`）。這就是「DB 層回滾」不列為選項的原因 |
| upstream-diff：env 移除 | 被移除的 `OMNIGENT_*` | add-on 有設的話，preflight 第 4 段會判定 |
| upstream-diff：協定常數、Frame、CAP | `SUPPORTED_FRAME_PROTOCOL_MAJOR` 等常數，以及新增或移除的 frame、capability | major 變了，舊 host 會被拒。frame、CAP 只有新增時，舊 host 不送也能用 |
| upstream-diff：tunnel 程式 | `host_tunnel.py`、`runner_tunnel.py`、`host/connect.py` 的變動量 | 常數沒變，連線行為也可能變（例：0.18 加了 30 秒的 hello 逾時）。有變動就拿使用者手上的舊版 host 實際連新 server 一次 |
| upstream-diff：web-ui 子路徑 | `basePath.ts`、`identity.ts`、`main.tsx`、`vite.config.ts` 的變動；`app.py` 的 `_rewrite_web_ui_index`、`_normalize_base_path` | 有變就要重讀 `ingress.md`，並在 HA 上驗登入與登出 |
| upstream-diff：auth | `accounts_auth.py` 等檔案的變動量 | 有變就在 HA 上確認：既有帳號照樣能登入、舊 cookie 不會被強制登出 |
| upstream-diff：`omnigent server` option | 新增或移除的 option | 移除的 option 如果 add-on 有用，preflight 第 2 段會判定 |

## build（Alpine／musl）

- **python 版本由 base image 的 Alpine 版本決定。**
  - base image 本身不含 Python，是 Dockerfile 自己 `apk add python3`。
  - `ghcr.io/hassio-addons/base/{arch}:20.2.0` 是 Alpine 3.23，裝到的是 Python 3.12.16。
  - Dockerfile 裡寫死了 `/usr/lib/python3.12/site-packages`：sed 的 `find` 路徑和 build 檢查都用到它。
  - base 換成 Python 3.13 以上的版本時，這兩處要一起改。base 升級和 omnigent 升級分開做。
- **pip 只釘 omnigent 本身，其他依賴沒有 lock。**
  - 實際裝進去的版本可能和上游 `uv.lock` 不同。例：0.1.17 裝的是 SQLAlchemy 2.1.8，上游鎖的是 2.0.50。
  - PR 的 image 與 merge 後發布的 image，也可能裝到不同版本。
  - 要固定的話，用上游 `uv.lock` 產生 constraints 檔，交給 `pip install -c`。
- **google-re2 會被清掉。**
  - `re2-dev` 會一併裝進 `py3-re2`，pip 判定 google-re2「already satisfied」就跳過。接著 `apk del .build-deps` 又把 `py3-re2` 一起刪掉（build log 有 `Purging py3-re2`）。
  - 結果是執行期沒有 google-re2。
    - 啟動時不會報錯：omnigent 的 policy registry 吞掉了 import 失敗。
    - 真的建立或使用 CEL policy 時，`cel_policy()` 會 raise `ImportError: cel-python is required … but is not installed`。訊息會誤導人以為是 cel-python 沒裝，實際缺的是 re2。
  - 要用 CEL 的話，在 runtime 的 `apk add` 明確加上 `py3-re2`，build 後用 `python3 -c 'import re2'` 檢查。
- **依賴的 musllinux wheel**：
  - 0.12–0.21 之間，只有 `google-re2` 和純 Python 的 `claude-agent-sdk` 沒有 wheel，新依賴只多了純 Python 的 `filelock`。
  - 新版若多了 native 依賴，用 uv 一次列出所有缺 wheel 的套件。
    - `pip download --only-binary` 不適合：它停在第一個缺 wheel 的套件，而 `claude-agent-sdk` 永遠會先失敗，後面的看不到。
    - 做法：建一個 override 檔，寫兩行把兩個已知缺口排除：`claude-agent-sdk; sys_platform == "never"`、`google-re2; sys_platform == "never"`。
    - 然後跑：`echo 'omnigent==<TO>' | uv pip compile - --python-platform x86_64-unknown-linux-musl --python-version 3.12 --only-binary :all: --override <override 檔>`。
    - 解得完，就代表其餘依賴都有 wheel。aarch64 換成 `aarch64-unknown-linux-musl` 再跑一次。
