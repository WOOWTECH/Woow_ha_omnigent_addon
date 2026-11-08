#!/usr/bin/env bash
# omnigent 上游兩個 release tag 之間，會影響本 add-on 的差異。
# 只讀上游 clone（會 fetch tags，NO_FETCH=1 可略過），不動工作樹。
#
# 用法：upstream-diff.sh <FROM_TAG> <TO_TAG> <UPSTREAM_DIR>
#   例：upstream-diff.sh v0.20.0 v0.21.0 ~/src/omnigent
#
# UPSTREAM_DIR 是 https://github.com/omnigent-ai/omnigent 的 clone。沒有就建一個不 checkout 的：
#   git clone --no-checkout --filter=blob:none https://github.com/omnigent-ai/omnigent.git <dir>
# 這種 clone 沒有工作樹，只用 show／ls-tree／diff／grep／archive。
#
# minor release 各在自己的 release/vX.Y.0 分支上，不互為祖孫（patch release 接在同一條分支）
# ⇒ 一律比兩個 tag 的 tree（git diff FROM TO），不用 FROM..TO 的 log。
#
# add-on 實際用到的耦合點（ingress 依賴、CLI flag、API、env、feature）由 addon-preflight.py 判定；
# 本腳本列出上游這段期間的變動，供「本次決定」判讀。
set -euo pipefail

FROM="${1:?用法：upstream-diff.sh <FROM_TAG> <TO_TAG> <UPSTREAM_DIR>}"
TO="${2:?用法：upstream-diff.sh <FROM_TAG> <TO_TAG> <UPSTREAM_DIR>}"
DIR="${3:?用法：upstream-diff.sh <FROM_TAG> <TO_TAG> <UPSTREAM_DIR>}"
cd "$DIR"
[ "${NO_FETCH:-}" = 1 ] || git fetch -q --tags origin
for t in "$FROM" "$TO"; do
  git rev-parse -q --verify "refs/tags/$t" >/dev/null || { echo "找不到 tag $t" >&2; exit 2; }
done
V=omnigent/db/migrations/versions
hr() { printf '\n=== %s ===\n' "$1"; }
# pipefail 下 diff／grep 的非 0 也會讓整條 pipeline 失敗，過濾一律經過這兩個 helper。
filt() { grep -E "$@" || true; }
or_none() { local out; out=$(cat); if [ -n "$out" ]; then printf '%s\n' "$out"; else echo "$1"; fi; }

hr "版本字串（pyproject.toml）"
for t in "$FROM" "$TO"; do
  printf '%s: %s\n' "$t" "$(git show "$t:pyproject.toml" | grep -m1 -E '^version *=')"
done

# --- alembic：每個 revision 一行「rev<TAB>down_revision(,分隔)<TAB>檔名」---
alembic_tsv() {
  git archive "$1" "$V" | python3 -I -c '
import re, sys, tarfile
tf = tarfile.open(fileobj=sys.stdin.buffer, mode="r|*")
for m in tf:
    if not (m.isfile() and m.name.endswith(".py")):
        continue
    t = tf.extractfile(m).read().decode()
    r = re.search(r"^revision\b[^=\n]*=\s*[\"\x27]([^\"\x27]+)[\"\x27]", t, re.M)
    d = re.search(r"^down_revision\b[^=\n]*=\s*(\([^)]*\)|[^\n]+)", t, re.M)
    if not r:
        continue
    downs = ",".join(re.findall(r"[\"\x27]([^\"\x27]+)[\"\x27]", d.group(1))) if d else ""
    print(f"{r.group(1)}\t{downs}\t{m.name.rsplit(chr(47), 1)[-1]}")
'
}
alembic_head() {
  alembic_tsv "$1" | awk -F'\t' '{r[$1]=1; n=split($2,a,","); for(i=1;i<=n;i++) d[a[i]]=1}
    END {for (k in r) if (!(k in d)) print k}'
}

hr "alembic head（TO 應該只有一行；多行＝多個 head）"
echo "$FROM: $(alembic_head "$FROM" | tr '\n' ' ')"
echo "$TO: $(alembic_head "$TO" | tr '\n' ' ')"

hr "alembic revision 增減（> 新增、< 移除；欄位＝rev、down_revision、檔名）"
{ diff <(alembic_tsv "$FROM" | sort) <(alembic_tsv "$TO" | sort) || true; } | filt '^[<>]' | or_none "（沒有增減）"

hr "omnigent/db 檔案變動（M＝既有 migration 或 db_models 被改）"
git diff --name-status "$FROM" "$TO" -- omnigent/db | cat

hr "新增 migration 的資料操作與不可重跑的 DDL（UPDATE／DELETE 會改資料；沒有 IF NOT EXISTS 的 add_column 在改回 alembic_version 後重升會撞錯）"
git diff -U0 "$FROM" "$TO" -- "$V" | filt '^(\+\+\+ |\+)' \
  | filt -n '^\+\+\+ |CONCURRENTLY|autocommit_block|LOCK TABLE|ALTER TABLE|ADD COLUMN|DROP (TABLE|COLUMN|INDEX|CONSTRAINT)|IF (NOT )?EXISTS|\b(UPDATE|DELETE FROM|INSERT INTO)\b|batch_alter_table|alter_column|add_column|drop_column|create_index|drop_index|create_unique_constraint|drop_constraint|op\.execute' \
  | or_none "（沒有命中）"

# --- env 名稱 ---
envs() { git grep -ohE '\bOMNIGENT_[A-Z0-9_]+' "$1" -- omnigent | sort -u; }
hr "OMNIGENT_* env：新增"
comm -13 <(envs "$FROM") <(envs "$TO") | or_none "（沒有）"
hr "OMNIGENT_* env：移除（add-on 有設的話新版會讀不到；addon-preflight.py 第 4 段會判定）"
comm -23 <(envs "$FROM") <(envs "$TO") | or_none "（沒有）"

hr "feature flag（名稱改動或移除；含數字的名稱也要抓，例如 automations_v2）"
ff() { git show "$1:omnigent/server/feature_flags.py" | grep -E '^\s+[A-Z][A-Z0-9_]* = "[a-z0-9_]+"$'; }
{ diff <(ff "$FROM") <(ff "$TO") || true; } | or_none "（沒有差異）"
hr "feature flag：default_enabled=True 的定義（TO）"
git show "$TO:omnigent/server/feature_flags.py" | python3 -I -c '
import re, sys
src = sys.stdin.read()
for block in re.findall(r"FeatureDefinition\((.*?)\n\s{4}\)", src, re.S):
    m = re.search(r"feature=Feature\.([A-Z0-9_]+)", block)
    if m and re.search(r"default_enabled\s*=\s*True", block):
        print(m.group(1))
' | or_none "（沒有）"

hr "host／runner 協定常數（去掉行號比對）"
protos() {
  git grep -nE '^SUPPORTED_FRAME_PROTOCOL_MAJOR\s*=|^SESSION_INIT_PROTOCOL_VERSION[^=]*=|frame_protocol_version=[0-9]+' "$1" -- omnigent \
    | grep -v tests | sed "s/^$1://" | sed -E 's/:[0-9]+:/:/'
}
diff <(protos "$FROM") <(protos "$TO") && echo "無變動（major 沒變不代表連線行為沒變，看下面的 frame／CAP／tunnel）"
hr "Frame 類別：新增、移除"
fc() { git show "$1:omnigent/host/frames.py" | grep -E '^class [A-Za-z0-9]+Frame\b' | sed -E 's/\(.*//; s/:$//' | sort -u; }
echo "新增：$(comm -13 <(fc "$FROM") <(fc "$TO") | sed 's/^class //' | tr '\n' ' ')"
echo "移除：$(comm -23 <(fc "$FROM") <(fc "$TO") | sed 's/^class //' | tr '\n' ' ')"
hr "capability（CAP_*）差異"
cap() { git show "$1:omnigent/host/frames.py" | grep -E '^CAP_[A-Z0-9_]+ *=' | sort -u; }
{ diff <(cap "$FROM") <(cap "$TO") || true; } | filt '^[<>]' | or_none "（沒有差異）"
hr "tunnel 握手與連線程式的變動量（有變就用舊 host 實連新 server 一次）"
git diff --stat "$FROM" "$TO" -- omnigent/server/routes/host_tunnel.py omnigent/server/routes/runner_tunnel.py \
  omnigent/host/connect.py omnigent/host/frames.py | cat
echo "新增的 tunnel 相關檔案：$(git diff --name-status "$FROM" "$TO" -- omnigent/server omnigent/host | awk '$1=="A" && /tunnel|handshake/ {print $2}' | tr '\n' ' ')"

hr "web-ui 子路徑與登入導向（ingress 依賴這些；有變就重跑 addon-preflight.py 並在 HA 上驗登入）"
git diff --stat "$FROM" "$TO" -- web/src/lib/basePath.ts web/src/lib/identity.ts web/src/main.tsx \
  web/vite.config.ts web/index.html | cat
echo "server 端 index 改寫與 base path（app.py 的相關函式在 TO 是否存在）："
for fn in _rewrite_web_ui_index _normalize_base_path _WEB_UI_API_FALLBACK_PREFIXES; do
  printf '  %s: FROM %s / TO %s\n' "$fn" \
    "$(git show "$FROM:omnigent/server/app.py" | grep -c "$fn" || true)" \
    "$(git show "$TO:omnigent/server/app.py" | grep -c "$fn" || true)"
done

hr "auth 與帳號（init-admin-claim 依賴 /v1/info 的 needs_setup 與 POST /auth/setup）"
git diff --stat "$FROM" "$TO" -- omnigent/server/routes/accounts_auth.py omnigent/server/auth.py \
  omnigent/server/accounts_store.py omnigent/server/accounts_config.py | cat

hr 'omnigent server 的 CLI option（新增、移除）'
# server 的 option 寫在 `@cli.group("server"…)` 與 `def server(` 之間。
opts() { git show "$1:omnigent/cli.py" | awk '/^@cli\.(group|command)\("server"/{f=1} /^def server\(/{f=0} f' \
  | grep -oE '"--[a-z][a-z0-9-]*(/--[a-z][a-z0-9-]*)?"' | sort -u; }
[ -n "$(opts "$TO")" ] || echo "（找不到 server 的 option 定義，cli.py 結構變了；改用 addon-preflight.py 第 2 段）"
echo "新增：$(comm -13 <(opts "$FROM") <(opts "$TO") | tr '\n' ' ')"
echo "移除：$(comm -23 <(opts "$FROM") <(opts "$TO") | tr '\n' ' ')"
