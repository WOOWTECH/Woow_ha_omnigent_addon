# Woow HA Omnigent add-on repository

A single-add-on HA repository packaging
[Omnigent](https://github.com/omnigent-ai/omnigent) as a Home Assistant OS
add-on.

## Add to Home Assistant

Supervisor → **Add-on Store** → three-dot menu → **Repositories** →
add:

```
https://github.com/WOOWTECH/Woow_ha_omnigent_addon
```

Then install **Woow Omnigent**.

## What's inside

| Add-on                             | Version | Slug             |
|------------------------------------|---------|------------------|
| [Woow Omnigent](./omnigent/README.md) | 0.1.0   | `woow-omnigent`  |

## Architecture

Server-only build. External runners register in over HTTP —
this add-on is only the orchestrator + Postgres + web UI.

```
┌────────── HAOS ──────────┐              ┌── LAN / Tailnet ──┐
│  Woow Omnigent add-on    │              │                   │
│  ┌────────────────────┐  │              │  ┌────────────┐   │
│  │ nginx  :8080  ─────┼──┼─ HA Ingress │  │ pi runner  │   │
│  │        (SPA proxy) │  │              │  │ on laptop  │   │
│  │ omnigent :8000 ────┼──┼──────────────┼──┤ or pi_agent│   │
│  │ postgres :5432 (lo)│  │   dial-in    │  │ add-on     │   │
│  └────────────────────┘  │              │  └────────────┘   │
└──────────────────────────┘              └───────────────────┘
```

See `omnigent/DOCS.md` for the operator docs.

## Sibling repos

- [`Woow_podman_omnigent_package`](https://github.com/WOOWTECH/Woow_podman_omnigent_package) — Podman-compose deployment
- [`Woow_k3s_omnigent_package`](https://github.com/WOOWTECH/Woow_k3s_omnigent_package) — Helm chart for k3s

## License

MIT.
