# Changelog

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
