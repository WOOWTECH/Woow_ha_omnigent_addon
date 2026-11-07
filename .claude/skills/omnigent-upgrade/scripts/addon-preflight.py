#!/usr/bin/env python3
"""add-on 對 omnigent 的耦合點，在目標版本還成不成立。

用法：addon-preflight.py <TO_VERSION> [--from <FROM_VERSION>] [--addon-dir <dir>] [--cache <dir>]
  例：addon-preflight.py 0.19.1
  FROM 預設讀 Dockerfile 的 ARG OMNIGENT_VERSION；addon-dir 預設是本 repo 的 omnigent/。

從 PyPI 下載 FROM 與 TO 的 wheel（核對 PyPI 公布的 sha256），不安裝、不執行，
只讀 wheel 內容，逐項比對 add-on 實際用到的東西：
  1. Dockerfile 的 web-ui sed 補丁：每條在 TO 的 assets/*.js 命中幾次（0 次 = 失效）
  2. svc-omnigent-server 傳給 `omnigent server` 的 flag
  3. rootfs 腳本呼叫的 HTTP API 路徑
  4. rootfs 腳本設定的 OMNIGENT_* env（FROM 有讀、TO 不讀 = 失效）
  5. config.yaml 預設的 feature flag（不認得的名稱會讓 server 起不來）
另外列出供判讀的資訊：requires-python 與 Requires-Dist 差異、default_enabled 的 feature、
WebSocket 路由、alembic revision、tunnel 協定 major、index.html 的資產形式。

結果：有 FAIL 時 exit 1（紅燈），全部 OK 時 exit 0。只用 Python 標準函式庫。
"""

import argparse
import hashlib
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
UA = "omnigent-addon-preflight"
ASSETS = "omnigent/server/static/web-ui/assets/"
INDEX = "omnigent/server/static/web-ui/index.html"

fails = []


def say(status, msg):
    print(f"  [{status}] {msg}")
    if status == "FAIL":
        fails.append(msg)


def hr(title):
    print(f"\n=== {title} ===")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def wheel(version, cache):
    """下載（或從 cache 取）omnigent==version 的 py3 wheel，核對 sha256 後回傳 ZipFile。"""
    meta = json.loads(fetch(f"https://pypi.org/pypi/omnigent/{version}/json"))
    files = [u for u in meta["urls"] if u["packagetype"] == "bdist_wheel"]
    if not files:
        sys.exit(f"PyPI 上的 omnigent {version} 沒有 wheel")
    f = files[0]
    path = cache / f["filename"]
    if not path.exists():
        cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fetch(f["url"]))
    data = path.read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != f["digests"]["sha256"]:
        path.unlink()
        sys.exit(f"{f['filename']} 的 sha256 與 PyPI 不符（{got}）；已刪掉 cache，請重跑")
    print(f"  {version}: {f['filename']} sha256 {got[:16]}… OK")
    return zipfile.ZipFile(io.BytesIO(data))


def text(zf, name):
    return zf.read(name).decode("utf-8", "replace")


def py_files(zf, prefix="omnigent/"):
    return [n for n in zf.namelist() if n.startswith(prefix) and n.endswith(".py")]


def grep_count(zf, needle, names):
    return sum(text(zf, n).count(needle) for n in names)


def metadata(zf):
    name = next(n for n in zf.namelist() if n.endswith(".dist-info/METADATA"))
    body = text(zf, name).split("\n\n", 1)[0]
    rp = re.search(r"^Requires-Python: *(.+)$", body, re.M)
    deps = sorted(re.findall(r"^Requires-Dist: *(.+)$", body, re.M))
    return (rp.group(1) if rp else "?"), [d for d in deps if "extra ==" not in d]


def sed_patterns(dockerfile):
    """Dockerfile 裡 `-e 's#PATTERN#REPLACEMENT#g'` 的 PATTERN（sed 分隔符是 #）。"""
    return re.findall(r"-e\s+'s#(.*?)#(.*?)#g'", dockerfile)


def route_defined(zf, names, path):
    """path 是否有 route：直接寫全路徑，或 router prefix + 其餘路徑。"""
    deco = r"\.(?:get|post|put|patch|delete|api_route|websocket)\(\s*[\"']{}[\"']"
    blob = "\n".join(text(zf, n) for n in names)
    if re.search(deco.format(re.escape(path)), blob):
        return "direct"
    parts = path.strip("/").split("/")
    for i in range(1, len(parts)):
        prefix = "/" + "/".join(parts[:i])
        rest = "/" + "/".join(parts[i:])
        if re.search(r"prefix\s*=\s*[\"']{}[\"']".format(re.escape(prefix)), blob) and re.search(
            deco.format(re.escape(rest)), blob
        ):
            return f"prefix {prefix} + {rest}"
    return None


def features(zf):
    src = text(zf, "omnigent/server/feature_flags.py")
    enum = dict(re.findall(r"^\s+([A-Z][A-Z0-9_]*) = \"([a-z0-9_]+)\"$", src, re.M))
    defaults = []
    for block in re.findall(r"FeatureDefinition\((.*?)\n\s{4}\)", src, re.S):
        m = re.search(r"feature=Feature\.([A-Z0-9_]+)", block)
        if m and re.search(r"default_enabled\s*=\s*True", block):
            defaults.append(enum.get(m.group(1), m.group(1)))
    return set(enum.values()), sorted(set(defaults))


def ws_routes(zf):
    out = set()
    for n in py_files(zf, "omnigent/server/"):
        out.update(re.findall(r"\.websocket\(\s*[\"']([^\"']+)", text(zf, n)))
    return sorted(out)


def alembic(zf):
    revs, downs, files = {}, set(), {}
    for n in zf.namelist():
        if not (n.startswith("omnigent/db/migrations/versions/") and n.endswith(".py")):
            continue
        src = text(zf, n)
        r = re.search(r"^revision\b[^=\n]*=\s*[\"']([^\"']+)[\"']", src, re.M)
        d = re.search(r"^down_revision\b[^=\n]*=\s*(\([^)]*\)|[^\n]+)", src, re.M)
        if not r:
            continue
        revs[r.group(1)] = n.rsplit("/", 1)[-1]
        if d:
            downs.update(re.findall(r"[\"']([^\"']+)[\"']", d.group(1)))
    heads = sorted(set(revs) - downs)
    return revs, heads


def frame_major(zf):
    for n in py_files(zf):
        m = re.search(r"^SUPPORTED_FRAME_PROTOCOL_MAJOR\s*=\s*(\d+)", text(zf, n), re.M)
        if m:
            return m.group(1)
    return "?"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("to_version")
    ap.add_argument("--from", dest="from_version")
    ap.add_argument("--addon-dir", type=Path, default=REPO / "omnigent")
    ap.add_argument(
        "--cache",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "omnigent-addon-preflight",
    )
    a = ap.parse_args()

    addon = a.addon_dir
    dockerfile = (addon / "Dockerfile").read_text()
    frm = a.from_version or re.search(r"^ARG OMNIGENT_VERSION=(\S+)", dockerfile, re.M).group(1)
    to = a.to_version
    rootfs = [p for p in (addon / "rootfs").rglob("*") if p.is_file()]
    scripts = {p: p.read_text(errors="replace") for p in rootfs}

    hr(f"wheel（FROM {frm} → TO {to}）")
    zf_from, zf_to = wheel(frm, a.cache), wheel(to, a.cache)

    hr("1. Dockerfile 的 web-ui sed 補丁（TO 命中 0 次 = 補丁失效；build 的檢查只要求至少一條命中，擋不住）")
    pats = sed_patterns(dockerfile)
    if not pats:
        say("OK", "Dockerfile 沒有 sed 補丁")
    for i, (pat, _) in enumerate(pats, 1):
        hits = {}
        for label, zf in (("FROM", zf_from), ("TO", zf_to)):
            js = [n for n in zf.namelist() if n.startswith(ASSETS) and n.endswith(".js")]
            hits[label] = [(n.rsplit("/", 1)[-1], text(zf, n).count(pat)) for n in js]
            hits[label] = [h for h in hits[label] if h[1]]
        n_from = sum(c for _, c in hits["FROM"])
        n_to = sum(c for _, c in hits["TO"])
        where = ", ".join(f"{f}×{c}" for f, c in hits["TO"]) or "-"
        say("OK" if n_to else "FAIL", f"P{i} FROM {n_from} 次 / TO {n_to} 次（{where}）：{pat[:90]}")
    for label, zf in (("FROM", zf_from), ("TO", zf_to)):
        idx = text(zf, INDEX) if INDEX in zf.namelist() else ""
        form = "./assets/（相對）" if 'src="./assets/' in idx else ("/assets/（絕對）" if 'src="/assets/' in idx else "?")
        print(
            f"  [info] {label} wheel 內靜態 index.html 的資產路徑 {form}；<base href> {'有' if '<base ' in idx else '無'}"
            "（server 啟動時可能再改寫，實際送出的 HTML 見 references/ingress.md）"
        )

    hr("2. svc-omnigent-server 傳給 `omnigent server` 的 flag")
    run = (addon / "rootfs/etc/s6-overlay/s6-rc.d/svc-omnigent-server/run").read_text()
    block = run[run.index("omnigent server"):] if "omnigent server" in run else ""
    flags = sorted(set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", block)))
    cli = "\n".join(text(zf_to, n) for n in py_files(zf_to) if "/cli" in n or n.endswith("cli.py"))
    for fl in flags:
        ok = re.search(r"[\"'/]{}[\"'/]".format(re.escape(fl)), cli)
        say("OK" if ok else "FAIL", f"{fl}")

    hr("3. s6 腳本呼叫的 HTTP API（nginx 的 proxy_pass 不算：它整段轉發，不依賴特定 route）")
    paths = set()
    for p, s in scripts.items():
        if "s6-overlay" in p.parts:
            paths.update(re.findall(r"http://127\.0\.0\.1:8000(/[A-Za-z0-9_./-]*)", s))
    server_py = py_files(zf_to, "omnigent/server/")
    for path in sorted(paths):
        how = route_defined(zf_to, server_py, path)
        say("OK" if how else "FAIL", f"{path}" + (f"（{how}）" if how else "：TO 找不到 route"))

    hr("4. rootfs 腳本設定的 OMNIGENT_* env（omnigent 原始碼裡的引用次數）")
    envs = set()
    for s in scripts.values():
        envs.update(re.findall(r"\b(OMNIGENT_[A-Z0-9_]+)", s))
    pf, pt = py_files(zf_from), py_files(zf_to)
    for e in sorted(envs):
        cf, ct = grep_count(zf_from, e, pf), grep_count(zf_to, e, pt)
        if cf and not ct:
            say("FAIL", f"{e}: FROM {cf} / TO {ct}（新版不讀了）")
        elif not cf and not ct:
            print(f"  [info] {e}: omnigent 不讀，是 add-on 自己用的")
        else:
            say("OK", f"{e}: FROM {cf} / TO {ct}")

    hr("5. config.yaml 預設的 feature flag")
    cfg = (addon / "config.yaml").read_text()
    m = re.search(r"^options:\n(?:  .*\n)*?  features: *\"?([^\"\n]*)\"?", cfg, re.M)
    wanted = [f.strip() for f in (m.group(1) if m else "").split(",") if f.strip()]
    known_to, defaults_to = features(zf_to)
    for f in wanted:
        name = f.lstrip("-")
        say("OK" if name in known_to else "FAIL", f"{f}" + ("" if name in known_to else "：TO 不認得，server 會起不來"))
    print(f"  [info] TO 預設開啟（不必列出、列出也關不掉）：{', '.join(defaults_to) or '無'}")
    known_from, _ = features(zf_from)
    print(f"  [info] TO 新增的 feature：{', '.join(sorted(known_to - known_from)) or '無'}")
    if known_from - known_to:
        print(f"  [info] TO 移除的 feature：{', '.join(sorted(known_from - known_to))}")

    hr("6. 判讀用資訊")
    rf, df = metadata(zf_from)
    rt, dt = metadata(zf_to)
    print(f"  requires-python：FROM {rf} / TO {rt}（add-on 的 python 版本由 base image 決定）")
    added, removed = sorted(set(dt) - set(df)), sorted(set(df) - set(dt))
    print(f"  Requires-Dist 新增或改 pin：{'; '.join(added) or '無'}")
    print(f"  Requires-Dist 移除或改 pin：{'; '.join(removed) or '無'}")
    wf, wt = ws_routes(zf_from), ws_routes(zf_to)
    print(f"  WebSocket 路由 TO：{', '.join(wt)}")
    if set(wt) != set(wf):
        print(f"    新增：{', '.join(sorted(set(wt) - set(wf))) or '無'}；移除：{', '.join(sorted(set(wf) - set(wt))) or '無'}")
    revf, hf = alembic(zf_from)
    revt, ht = alembic(zf_to)
    new = sorted(revt[r] for r in set(revt) - set(revf))
    gone = sorted(revf[r] for r in set(revf) - set(revt))
    print(f"  alembic：FROM {len(revf)} 個（head {','.join(hf)}）→ TO {len(revt)} 個（head {','.join(ht)}）")
    if len(ht) != 1:
        say("FAIL", f"TO 有 {len(ht)} 個 alembic head")
    print(f"    新增 {len(new)} 個：{', '.join(new) or '無'}")
    if gone:
        say("FAIL", f"TO 移除了 {len(gone)} 個 revision：{', '.join(gone)}（跨版升級可能接不上）")
    print(f"  tunnel 協定 major：FROM {frame_major(zf_from)} / TO {frame_major(zf_to)}（不同 = 舊 host 會被拒）")

    hr("結果")
    if fails:
        print(f"  紅燈：{len(fails)} 項 FAIL")
        for f in fails:
            print(f"   - {f}")
        sys.exit(1)
    print("  綠燈：耦合點都還成立（ingress 的實際行為仍要在 HA 上驗）")


if __name__ == "__main__":
    main()
