#!/bin/sh
# 離線測試 rootfs/usr/local/bin/pg-wal-rescue：還原「沒有 pg_wal 的備份」後，Postgres 能不能再起來。
#
# 用 PostgreSQL 16 的執行檔（與 add-on 的 postgresql16 同一個 major）在暫存目錄建 cluster，
# 以目前的使用者執行，不需要 root、網路或 docker。每個案例複製同一份基底資料：
#   1. 乾淨關機、WAL 在         -> 什麼都不做，pg_control 不變，起得來，資料在
#   2. 乾淨關機、pg_wal 清空     -> 紅燈證明：不救就 PANIC；救了之後起得來，資料在
#   3. 乾淨關機、pg_wal 整個不在 -> 救了之後起得來，資料在
#   4. 非乾淨關機、pg_wal 清空   -> exit 1、說明原因，pg_control 不變（不加 -f 硬做）
#   5. 非乾淨關機、WAL 在        -> 什麼都不做，crash recovery 照常，資料在
#   6. 還沒 initdb 的目錄         -> 什麼都不做
# 另外檢查：config.yaml 不再排除 pg_wal、init-postgres 在啟動 postgres 前呼叫它、Dockerfile 給它執行權限。
#
# 「pg_wal 清空」照 Supervisor 套用 backup_exclude 的結果：pg_wal 目錄留著，裡面的檔案與
# archive_status 都不在。
#
# PGBIN 指定 PostgreSQL 16 的 bin 目錄（預設 /usr/lib/postgresql/16/bin，Ubuntu 的位置）。
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RESCUE="$ROOT/rootfs/usr/local/bin/pg-wal-rescue"
INIT_PG="$ROOT/rootfs/etc/s6-overlay/s6-rc.d/init-postgres/run"
PGBIN=${PGBIN:-/usr/lib/postgresql/16/bin}
PORT=${TEST_PGPORT:-54329}

for b in initdb pg_ctl pg_controldata pg_resetwal psql; do
    [ -x "$PGBIN/$b" ] || { echo "FAIL harness: $PGBIN/$b not found; set PGBIN to a PostgreSQL 16 bin directory"; exit 1; }
done
"$PGBIN/pg_ctl" --version | grep -q ' 16\.' || { echo "FAIL harness: $PGBIN is not PostgreSQL 16"; exit 1; }

TMP=$(mktemp -d)
cleanup() {
    for d in "$TMP"/*/; do
        [ -f "${d}postmaster.pid" ] && "$PGBIN/pg_ctl" -D "$d" -m immediate -w stop >/dev/null 2>&1 || true
    done
    rm -rf "$TMP"
}
trap cleanup EXIT
fail=0

pass() { echo "PASS $1"; }
bad()  { echo "FAIL $1"; fail=1; }

start() { # dir -> 0 起得來
    "$PGBIN/pg_ctl" -D "$1" -o "-k $TMP -p $PORT -c listen_addresses=''" -l "$1.log" -w -t 30 start >/dev/null 2>&1
}
stop() { # dir mode
    "$PGBIN/pg_ctl" -D "$1" -m "$2" -w -t 30 stop >/dev/null 2>&1
}
q() { "$PGBIN/psql" -X -h "$TMP" -p "$PORT" -d postgres -tAc "$1"; }
state() { LC_ALL=C "$PGBIN/pg_controldata" -D "$1" | sed -n 's/^Database cluster state: *//p'; }
rescue() { # dir -> 把輸出寫到 dir.rescue，回傳 exit code
    rc=0; ${TEST_SH:-sh} "$RESCUE" "$PGBIN" "$1" > "$1.rescue" 2>&1 || rc=$?
    return $rc
}
data_ok() { # dir label
    if start "$1"; then
        got=$(q "SELECT string_agg(v, ',' ORDER BY v) FROM t" 2>/dev/null || true)
        stop "$1" fast || true
        [ "$got" = "after-checkpoint,kept" ] && return 0
        echo "    | data: '$got' (want 'after-checkpoint,kept')"
    else
        sed 's/^/    | /' "$1.log" | tail -5
    fi
    return 1
}

# --- 基底資料 ----------------------------------------------------------------
"$PGBIN/initdb" -D "$TMP/base" --encoding=UTF8 --locale=C >/dev/null 2>&1 || { echo "FAIL harness: initdb"; exit 1; }
start "$TMP/base" || { echo "FAIL harness: base cluster does not start"; cat "$TMP/base.log"; exit 1; }
q "CREATE TABLE t(v text); INSERT INTO t VALUES ('kept'); CHECKPOINT; INSERT INTO t VALUES ('after-checkpoint')" >/dev/null
stop "$TMP/base" fast
[ "$(state "$TMP/base")" = "shut down" ] || { echo "FAIL harness: base is not cleanly shut down"; exit 1; }

# 非乾淨關機的版本：寫入後用 immediate stop，等於斷電／被 SIGKILL。
cp -a "$TMP/base" "$TMP/unclean"
start "$TMP/unclean" || { echo "FAIL harness: unclean copy does not start"; exit 1; }
q "INSERT INTO t VALUES ('unclean-only')" >/dev/null
stop "$TMP/unclean" immediate
[ "$(state "$TMP/unclean")" = "in production" ] || { echo "FAIL harness: unclean copy is not in production state"; exit 1; }

# --- 1. 乾淨關機、WAL 在 -------------------------------------------------------
cp -a "$TMP/base" "$TMP/c1"; cp "$TMP/c1/global/pg_control" "$TMP/c1.ctl"
if rescue "$TMP/c1" && ! grep -q 'rebuilding' "$TMP/c1.rescue" && cmp -s "$TMP/c1/global/pg_control" "$TMP/c1.ctl" && data_ok "$TMP/c1"; then
    pass "clean shutdown, WAL present: nothing done, starts, data kept"
else
    bad "clean shutdown, WAL present"; sed 's/^/    | /' "$TMP/c1.rescue"
fi

# --- 2. 乾淨關機、pg_wal 清空（0.1.17 以前的備份還原後） ----------------------
cp -a "$TMP/base" "$TMP/c2"; rm -rf "$TMP/c2/pg_wal/"* "$TMP/c2/pg_wal/".[!.]* 2>/dev/null || true
cp -a "$TMP/c2" "$TMP/c2red"
if start "$TMP/c2red"; then
    bad "red proof: a cluster with an empty pg_wal started without the rescue (the test no longer reproduces the bug)"
    stop "$TMP/c2red" immediate || true
elif grep -q 'could not locate a valid checkpoint record' "$TMP/c2red.log"; then
    pass "red proof: without the rescue, an empty pg_wal stops Postgres with the PANIC"
else
    bad "red proof: start failed, but not with the expected PANIC"; sed 's/^/    | /' "$TMP/c2red.log" | tail -5
fi
if rescue "$TMP/c2" && grep -q 'rebuilding the WAL' "$TMP/c2.rescue" && data_ok "$TMP/c2"; then
    pass "clean shutdown, pg_wal emptied: WAL rebuilt, starts, data kept"
else
    bad "clean shutdown, pg_wal emptied"; sed 's/^/    | /' "$TMP/c2.rescue"
fi
# 救過一次後再開機，不會再動它。
if rescue "$TMP/c2" && ! grep -q 'rebuilding' "$TMP/c2.rescue"; then
    pass "after the rescue, the next start does nothing"
else
    bad "after the rescue, the next start still acts"; sed 's/^/    | /' "$TMP/c2.rescue"
fi

# --- 3. 乾淨關機、pg_wal 整個不在 ---------------------------------------------
cp -a "$TMP/base" "$TMP/c3"; rm -rf "$TMP/c3/pg_wal"
if rescue "$TMP/c3" && grep -q 'rebuilding the WAL' "$TMP/c3.rescue" && data_ok "$TMP/c3"; then
    pass "clean shutdown, no pg_wal directory: WAL rebuilt, starts, data kept"
else
    bad "clean shutdown, no pg_wal directory"; sed 's/^/    | /' "$TMP/c3.rescue"
fi

# --- 4. 非乾淨關機、pg_wal 清空 -----------------------------------------------
cp -a "$TMP/unclean" "$TMP/c4"; rm -rf "$TMP/c4/pg_wal/"* 2>/dev/null || true
cp "$TMP/c4/global/pg_control" "$TMP/c4.ctl"
rc=0; rescue "$TMP/c4" || rc=$?
if [ "$rc" = 1 ] && grep -q 'FATAL: Postgres was not shut down cleanly' "$TMP/c4.rescue" && cmp -s "$TMP/c4/global/pg_control" "$TMP/c4.ctl"; then
    pass "unclean shutdown, pg_wal emptied: stops with exit 1 and leaves pg_control alone"
else
    bad "unclean shutdown, pg_wal emptied: exit $rc"; sed 's/^/    | /' "$TMP/c4.rescue"
fi

# --- 5. 非乾淨關機、WAL 在 -----------------------------------------------------
cp -a "$TMP/unclean" "$TMP/c5"
if rescue "$TMP/c5" && ! grep -q 'rebuilding' "$TMP/c5.rescue" && start "$TMP/c5"; then
    got=$(q "SELECT count(*) FROM t WHERE v = 'unclean-only'")
    stop "$TMP/c5" fast || true
    if [ "$got" = 1 ]; then
        pass "unclean shutdown, WAL present: nothing done, crash recovery keeps the last write"
    else
        bad "unclean shutdown, WAL present: the last write is gone"
    fi
else
    bad "unclean shutdown, WAL present"; sed 's/^/    | /' "$TMP/c5.rescue" "$TMP/c5.log" | tail -8
fi

# --- 6. 還沒 initdb ------------------------------------------------------------
mkdir "$TMP/c6"
if rescue "$TMP/c6" && [ ! -s "$TMP/c6.rescue" ] && [ -z "$(ls -A "$TMP/c6")" ]; then
    pass "empty data directory: nothing done"
else
    bad "empty data directory"; sed 's/^/    | /' "$TMP/c6.rescue"
fi

# --- 打包面 ----------------------------------------------------------------------
if grep -q 'pg_wal' "$ROOT/config.yaml" && sed -n '/^backup_exclude:/,/^[a-z]/p' "$ROOT/config.yaml" | grep -q '^ *- .*pg_wal'; then
    bad "config.yaml still excludes pg_wal from backups"
else
    pass "config.yaml keeps pg_wal in backups"
fi
rescue_line=$(grep -n 'pg-wal-rescue' "$INIT_PG" | grep -v '^[0-9]*: *#' | head -1 | cut -d: -f1)
start_line=$(grep -n 'pg_ctl -D .* start' "$INIT_PG" | head -1 | cut -d: -f1)
if [ -n "$rescue_line" ] && [ -n "$start_line" ] && [ "$rescue_line" -lt "$start_line" ]; then
    pass "init-postgres runs pg-wal-rescue before it starts Postgres"
else
    bad "init-postgres does not run pg-wal-rescue before pg_ctl start (rescue line '$rescue_line', start line '$start_line')"
fi
if grep -q '/usr/local/bin/pg-wal-rescue' "$ROOT/Dockerfile"; then
    pass "Dockerfile marks pg-wal-rescue executable"
else
    bad "Dockerfile does not chmod pg-wal-rescue"
fi
if head -1 "$RESCUE" | grep -qx '#!/bin/sh'; then
    pass "pg-wal-rescue is a POSIX sh script"
else
    bad "pg-wal-rescue must start with #!/bin/sh (busybox in the add-on)"
fi

echo "pg_wal size of the base cluster: $(du -sh "$TMP/base/pg_wal" | cut -f1)"
[ "$fail" = 0 ] && echo "all checks passed" || { echo "some checks failed"; exit 1; }
