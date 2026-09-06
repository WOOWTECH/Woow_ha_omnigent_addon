# Changelog

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
