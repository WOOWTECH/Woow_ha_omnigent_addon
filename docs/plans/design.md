# Design — Woow HA Omnigent add-on

## Decision: Option 3 — server-only

We ship the Omnigent orchestrator + Postgres in the add-on. No pi CLI, no
claude/codex CLI, no bundled runner.

External runners register themselves:

```
omnigent host --server http://homeassistant.local:8000
```

### Why not bundle a runner?

1. **Image size.** pi + claude + codex + node runtimes push the image past
   1 GB. HA add-on stores prefer <400 MB.
2. **State conflicts.** The Podman + k3s deployments both hit "pi extension
   conflict" bugs where seeded plugins register duplicate MCP tool names.
   Externalising the runner lets each host manage its own `~/.pi-agent/`.
3. **Multi-source reality.** The user already runs runners in multiple
   places (Podman on `.197`, k3s pi1/pi4/pi5, laptop, `woow_ha_pi_agent`
   add-on). The add-on is one more hub, not the only one.

### Trade-off accepted

- First-time users see an empty host list on the web UI until they wire
  up at least one runner. `DOCS.md` calls this out.

## Service graph (s6-overlay)

```
init-addon-config  ─┐
                    ├─▶ init-postgres ─▶ svc-postgres ─┐
                                                        ├─▶ init-omnigent-config
                                                        │      ▼
                                                        │   svc-omnigent-server ─┬─▶ init-admin-claim
                                                        │                        └─▶ nginx
```

All 4 oneshots + 3 longruns live under `rootfs/etc/s6-overlay/s6-rc.d/`.
`user/contents.d/` enumerates the 7 bundle members. Dependency ordering
via `dependencies.d/`.

## Networking

| Listener              | Bind         | Exposed via                    |
|-----------------------|--------------|--------------------------------|
| `postgres`            | 127.0.0.1:5432 | internal only                |
| `omnigent server`     | 0.0.0.0:8000 | HAOS `ports:` → LAN + CF tunnel|
| `nginx` (Ingress SPA) | 0.0.0.0:8080 | HAOS `ingress:` → 172.30.32.2 only |

`nginx` restricts port 8080 to Supervisor (`allow 172.30.32.2; deny all`).
Same trick as the n8n add-on.

## Path-rewriting

React SPA emits root-absolute URLs. HA Ingress serves it under
`/api/hassio_ingress/<slug>/`. We inject `<base href>` + a JS shim that
patches `fetch`, `XMLHttpRequest.open`, `history.pushState/replaceState`,
and `WebSocket` to prepend the ingress path.

Copied wholesale from `Woow_ha_n8n`'s Vue equivalent — only the injection
selector (`<head>`) changes.

## Bundled Postgres — why in-image, not a MariaDB dep?

- Omnigent needs Postgres, not MariaDB.
- Officially available "PostgreSQL add-on" (via the addon store) is not a
  first-party HA add-on; adding a dependency on a community add-on
  creates install friction.
- Bundling in-image matches how the k3s chart works (StatefulSet Postgres
  next to the server).
- Data lives at `/data/postgres`, persisted across restarts, included in
  add-on backups.

## What we deliberately did NOT do

- **No pgbouncer.** localhost connection, single-tenant, no need.
- **No SSL for postgres.** loopback only.
- **No sqlite alt-mode.** upstream 0.12.0 asyncpg codepath doesn't test on
  sqlite; not worth the compat risk.
- **No runner tunnel supervisor inside the addon.** external runners own
  their own reconnect loop.
