# Changelog

## 0.1.10 — 2026-09-06

- Minimise `.build-deps` to `build-base + abseil-cpp-dev + re2-dev`.
  On base 20.2.0, `openssl-dev` demands libcrypto3-3.5.8 while world
  pins 3.5.6, which fails apk resolution. Everything else in
  omnigent's dep tree ships musllinux wheels so the extra -dev
  packages weren't needed anyway.

## 0.1.9 — 2026-09-06

- Pin base image to `hassio-addons/base:20.2.0` (Alpine 3.22 + Python
  3.12) instead of `21.0.4` (Alpine 3.23 + Python 3.14). Upstream
  `anyio` fails to import on 3.14:
  `ImportError: cannot import name 'sentinel' from 'typing_extensions'`
  — the module renamed it to `Sentinel` in newer versions and anyio's
  release used with omnigent 0.12.0 wasn't updated yet.

## 0.1.8 — 2026-09-06

- `svc-omnigent-server` now sources `/var/run/omnigent.env` and passes
  `--host 0.0.0.0 --port 8000 --database-uri postgresql+psycopg://...
  --no-open` explicitly. `with-contenv` snapshots
  `/run/s6/container_environment/` at boot before `init-addon-config`
  writes to it, so omnigent was falling back to defaults and binding
  `127.0.0.1:6767` on sqlite instead.

## 0.1.7 — 2026-09-06

- Add `abseil-cpp-dev` + `re2-dev` to the build-deps group so
  `google-re2`'s source build can find `absl/strings/string_view.h`.
  Base image also bumped to Python 3.14 which forces a fresh build
  even where wheels exist for older cpython.

## 0.1.6 — 2026-09-06

- Actually install `omnigent`. Prior builds shipped an image without
  the Python package because `google-re2` (transitive dep via
  `cel-python`) has no musllinux wheel and needs `c++` to build from
  source — and the Dockerfile swallowed the failure with `|| true`,
  producing an "successful" image with no server binary.
- Add throwaway `.build-deps` (build-base + python3-dev + libffi-dev +
  openssl-dev + postgres-dev) around the pip install, then `apk del`.
- Split the `rm ~/.omnigent` cleanup into its own RUN so a real
  install failure now aborts the build instead of vanishing.

## 0.1.5 — 2026-09-06

- Serve ingress path-rewriting shim from a static `_ingress-shim.js`
  file instead of inlining the JS in `sub_filter`. nginx tried to
  parse `$/` inside the JS WebSocket regex as a variable reference
  and refused to load the config (`invalid variable name`).
- `svc-omnigent-server` now exports an explicit PATH before exec — the
  s6-oneshot env doesn't inherit it, so `omnigent: not found` was
  crashing the longrun in a restart loop.

## 0.1.4 — 2026-09-06

- Pre-create `/run/postgresql` (Alpine's default unix socket dir) so the
  postmaster can bind on first boot. Fresh addon containers don't ship
  this dir, causing `pg_ctl start` to silently fail.
- Dump `/tmp/pg-bootstrap.log` on postgres start failure so future
  boot errors surface in `ha apps logs` instead of vanishing.

## 0.1.3 — 2026-09-06

- Drop `bashio::config` in favour of `jq` on `/data/options.json`. On
  this base image the Supervisor API token isn't reliably wired into
  the s6-oneshot env, so every `bashio::config` call returned "Unable
  to access the API, forbidden" and the container refused to boot. The
  on-disk options.json is authoritative anyway.
- Switch all `run` scripts to `#!/command/with-contenv sh` for uniform
  env inheritance from `/run/s6/container_environment/`.
- Fix env dir path: was `/var/run/s6-rc/container_environment` (wrong),
  now `/run/s6/container_environment` (what s6-overlay v3 actually reads).

## 0.1.2 — 2026-09-06

- Re-enable `hassio_api: true`. `bashio::config` fetches
  `/data/options.json` via the Supervisor API, so disabling it breaks
  `init-addon-config` at boot (`Unable to access the API, forbidden`).

## 0.1.1 — 2026-09-06

- `base_url` schema relaxed from `url?` to `str?` so an empty string
  (the default when the operator hasn't set up a CF tunnel yet) doesn't
  fail Supervisor validation.

## 0.1.0 — 2026-09-06

- Initial release.
- Bundles omnigent 0.12.0 + PostgreSQL 16 + nginx via s6-overlay.
- HA Ingress panel with React SPA path rewriting shim (same trick as
  the sibling n8n add-on).
- LAN port 8000 exposed for external runner dial-in (`omnigent host
  --server http://homeassistant.local:8000`).
- Auto admin claim via `POST /auth/setup` on first boot (409 tolerant).
- `env_vars` schema field for arbitrary passthrough (e.g. API keys).
- **Not bundled:** pi / claude / codex CLI, omnigent runner — external
  hosts register themselves. Rationale: keeps the addon image <400 MB
  and matches how the k3s deployment already works.
