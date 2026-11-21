# Disposable amd64 HAOS acceptance

`Test real HAOS add-on acceptance / HAOS acceptance (amd64)` runs on ordinary
pull requests. A passing local harness test is not evidence of a booted HAOS
guest. Acceptance requires this job's successful, complete JSON receipt for the
reviewed PR head. Missing KVM, space, artifacts or readiness fails the job.

## Bootstrap and resource bounds

The guest follows the official [HAOS QEMU tests](https://github.com/home-assistant/operating-system/blob/dev/tests/qemu-strategy.yaml)
and [CONFIG drive developer SSH bootstrap](https://contributors.home-assistant.io/os/operating-system/debugging/).
It uses the checksum-pinned official HAOS 18.1 OVA qcow2, OVMF, virtio and KVM.
A new key, CONFIG drive and writable disk are created for each job. Supervisor
and Core follow HAOS's normal first-boot installation. No prior guest is reused.

The job grants only its current user access to `/dev/kvm`, then requires both
`KVM_GET_API_VERSION` and `KVM_CREATE_VM` to work. There is no software CPU
fallback. GitHub does not guarantee nested virtualization on hosted runners;
an unavailable accelerator is a failed acceptance prerequisite.

| Resource | Bound |
| --- | --- |
| Runner | Public Ubuntu 22.04 amd64 hosted runner |
| Guest | 2 vCPU, 6 GiB RAM |
| Guest disk | 32 GiB virtual qcow2, sparse physical allocation |
| Host disk | At least 12 GiB physically free before downloads; 2 GiB reserve monitored every 5 seconds |
| Time | 60-minute job; 50-minute acceptance process; individual commands/readiness also bounded |
| Networking | QEMU user networking; all forwarded ports and the registry listen on loopback |

Sparse capacity is not free physical capacity. The physical reserve aborts the
run if image pulls, backup data or guest writes consume it. The harness does not
delete preinstalled runner software to make space. A failed preflight requires a
runner with the stated capacity. HAOS release downloads, GitHub artifact reads,
the public baseline GHCR image, Docker Hub registry image and HAOS's normal
Supervisor/Core downloads must be reachable.

## Candidate and baseline identity

The baseline is version 0.1.23 with both its config source/checksum and published
amd64 image digest fixed in `host.py`. The candidate is the current PR's existing
amd64 build tar, downloaded without publishing a PR tag or rebuilding it.

Acceptance checks out the explicit PR head. The existing producer retains its
default synthetic merge checkout and writes `producer-source.json` beside its
image artifact. Acceptance requires:

1. A successful `build (amd64)` in the matching PR-head producer run.
2. Image and source artifacts from that build job's actual `run_attempt`
   (including failed-jobs reruns), with GitHub artifact SHA256 validation.
3. The producer's `omnigent` Git tree to equal the PR-head tree. A merge checkout
   with different add-on content fails; no exact-commit equivalence is claimed.
4. Matching config, Dockerfile and artifact-pin hashes, plus the producer's
   recorded Docker image ID.
5. The actual Supervisor container to run that image ID, with the installed wheel
   verified against the reviewed core source and wheel receipt inside the guest.

The source receipt is `haos-image-source-attempt-<attempt>`. The image remains
the Store-compatible `upgrade-comparison-image-woow-omnigent-attempt-<attempt>`;
its format is unchanged. Acceptance waits up to 15 minutes for a matching build.
If the build exceeds that window, rerun the failed acceptance job after it
finishes. An expired image requires a producer rerun.

Supervisor pulls the unchanged old/new images from a job-owned loopback registry
through a temporary SSH reverse forward. Only the disposable local add-on config's
`image` reference changes. Both versions use `local_woow-omnigent`, preserving
Supervisor's real data directory across the update.

## Checks and receipt

The sequence installs 0.1.23 through Supervisor, seeds the existing upgrade
fixtures, creates a Supervisor cold backup, updates to 0.1.24, and checks the
task/agent fields, member login, session ACLs and artifact bytes.

The real HA owner signs in through Chromium and opens the HA sidebar Ingress.
The browser checks add-on login, lazy settings pages, MCP creation/copy, one-time
URL hiding, expired-session navigation and logout. A separate localhost HTTPS
proxy serves the complete `/omnigent` deployment prefix. Its generated CA is
explicitly trusted by the MCP client: certificate verification stays enabled.
The copied URL must equal the API's `connection_url`; cookie-free requests check
initialize, 15 tools, session/history ACLs and active/revoked profiles. GET and
DELETE must return the candidate's stateless-transport 405 response.

Actual Supervisor backup/restore checks include:

- Both cold backups must restart the running add-on container.
- Revoke a saved active token after the candidate backup. Restore that backup;
  the original active URL must work again and the earlier revoked URL stay denied.
- Cleanly power off HAOS, start a new QEMU process with the same disk, require a
  changed kernel boot ID, and recheck data plus the original profile URLs.
- Restore the baseline backup; require the old image/version, original data,
  member credentials and absence of the candidate MCP API.

Only `haos-acceptance.json` is uploaded. It includes controlled stage outcomes,
artifact/image/source identities, physical resource counts, Supervisor version,
database version and migration head. Failures contain a stage, exception type and
source line, without exception text. Passwords, keys, token URLs, backups, browser
traces, screenshots and raw command/guest logs remain private temporary files.
Cleanup terminates the harness's processes and verifies the registry ownership
label before deleting it and its anonymous volume. Job teardown removes the
ephemeral runner and remaining Docker images.

## Limits

This covers actual amd64 HAOS/Supervisor on a newly provisioned CI guest. It does
not establish native arm64 runtime, Internet-public HTTPS or Pi connectivity,
distinct MCP-capable version A-to-B migration, external runner/terminal operation,
LAN browser login, or long-lived SSE behavior. The baseline predates MCP.
Poweroff/container-restart checks also do not assert the exact PostgreSQL shutdown
log lines in the manual H3 checklist. These remain separate acceptance evidence.

## Local harness validation

Install `requirements.txt`, nginx and OpenSSL, then run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=omnigent/tests \
  python -m unittest discover -s omnigent/tests -p 'test_haos_harness.py' -v
```

`NGINX_BINARY` can select a locally installed nginx executable. The tests exercise
artifact selection, source binding, fail-closed preflight, ownership checks,
sanitized failure output and a real TLS proxy against a small HTTP fixture.
They neither download HAOS nor launch a VM. The acceptance entry point itself
requires the GitHub Actions environment and is invoked only by the PR workflow.
