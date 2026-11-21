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

The harness saves the original `/dev/kvm` ACL and device identity before granting
only its current UID read/write access, then requires both `KVM_GET_API_VERSION`
and `KVM_CREATE_VM` to work. QEMU runs as the unprivileged runner. Before every
actual QEMU launch, including cold boot, it drains pending udev events with a
five-second settle bound and refreshes that UID's ACL. Each refresh verifies the
same character device and effective access; its final grant becomes the access
comparison baseline. Raw ACLs and numeric user/group identities stay private.
There is no software CPU
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
   recorded Docker image ID cryptographically linked to the saved image config.
   Docker's [containerd store reports a manifest ID](https://github.com/moby/moby/blob/master/daemon/containerd/image_inspect.go),
   while its legacy store reports a config ID. Before loading the verified tar,
   acceptance hashes its config bytes. A differing producer ID must equal the
   archive's single image manifest descriptor, whose bytes, digest, size and
   config reference are verified. Only that linked manifest or config digest is
   accepted for the loaded image; labels/version alone never establish identity.
   Duplicate members, unexpected tags, altered descriptors and unlinked IDs fail.
5. After each local registry push, hash the served manifest bytes against its
   `Docker-Content-Digest` header and verify its config descriptor and config blob
   against the anchored config digest. A push may rewrite the manifest, so the
   pushed digest is measured independently of the producer digest. The candidate
   anchor comes from its verified CI archive. The baseline anchor comes from an
   export of the image pulled by its fixed digest; this temporary tar is deleted
   immediately after verification. The actual Supervisor container must report
   either that config digest or that verified pushed manifest digest. Both versions
   use this check at install, before/after cold backup, restore and cold boot,
   with the installed candidate wheel also verified inside the guest.

Fresh [HAOS 18.1 enables the containerd image store](https://github.com/home-assistant/operating-system/blob/18.1/buildroot-external/rootfs-overlay/usr/libexec/docker-prepare).
Its container image ID can therefore differ from the runner's config ID. The
receipt records the independently verified registry/config digests for each
version; an unrelated ID or missing registry proof always fails. Registry metadata
requests stay on the job's loopback listener, disable redirects and environment
proxies, have a ten-second timeout, and read at most 1 MiB per object.

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
Boot diagnostics distinguish QEMU process exit, guest SSH wait and Supervisor
wait. The receipt includes process exit codes, attempt counts and allowlisted
error/console marker labels. During Supervisor readiness, both `/supervisor/info`
and `/info` retain separate HTTP statuses, curl/SSH exit codes and timeout flags.
The guest probe records Docker/curl/jq availability, container inspection,
token lookup, network count and address lookup exit codes, plus health and an
allowlisted system state. These diagnostics are captured before cleanup; raw
commands, response bodies, IP addresses, tokens and QEMU/SSH output stay private.
Supervisor supplies the CLI plugin's token through its
[container environment](https://github.com/home-assistant/supervisor/blob/main/supervisor/docker/cli.py).
Both readiness and later Supervisor operations read only that runtime variable
using `docker exec hassio_cli printenv SUPERVISOR_TOKEN`, captured inside the
guest shell. A stopped CLI or absent/empty token remains retryable during startup;
missing executables and jq usage/compile errors fail immediately. Health and root
state still require successful authenticated HTTP responses from both endpoints.
An exited QEMU process fails immediately instead of consuming the SSH timeout.
Registry diagnostics distinguish the pinned image pull, container launch/state,
host HTTP readiness, guest forwarding, baseline/candidate tag/push/inspect,
metadata parsing, architecture/version checks and final producer/source guards.
The implicit Docker pull is split into a bounded explicit pull and a launch with
`--pull=never`, using the same pinned image and loopback binding. Each boundary
retains a pass/fail boolean. Only fixed stage names, command exit codes, timeout
flags and allowlisted exception classes/error markers are reported, including
certificate, missing-path and connection failures when Docker emits those markers.
Container creation/running state and exit code, host HTTP status and guest
probe/SSH tunnel exit codes distinguish server readiness from forwarding. The
loopback registry uses HTTP, so it has no local certificate/key paths to report.
Command arguments, output, exception text and registry response bodies stay
private; these checkpoints do not weaken any image identity or transport requirement.
Cleanup terminates the harness's processes and verifies the registry ownership
label before deleting it and its anonymous volume. A launch is marked attempted
before invoking Docker; container creation remains unknown if that command fails
or times out. Cleanup inspects the exact unique container name after every launch
attempt, including failed launches. An inspect error requires a successful empty
exact-name listing to confirm absence. Ownership mismatch or lookup failure
preserves the container and fails cleanup; only the matching ownership label
permits removal. Separate sanitized cleanup status preserves the original failure
stage. After QEMU is reaped, cleanup
settles pending udev events and restores the one original ACL only if the original
character device, inode, device numbers, owner and group still match. ACL-related
mode changes are excluded from identity. A mismatch skips the ACL write and fails
cleanup; a failed settle still attempts restoration on the verified original
device and remains a failure. The receipt contains only access/ACL-change booleans
and fixed cleanup status codes. Job teardown removes the
ephemeral runner and remaining Docker images.

## Limits

This covers actual amd64 HAOS/Supervisor on a newly provisioned CI guest. It does
not establish native arm64 runtime, Internet-public HTTPS or Pi connectivity,
distinct MCP-capable version A-to-B migration, external runner/terminal operation,
LAN browser login, or long-lived SSE behavior. The baseline predates MCP.
Poweroff/container-restart checks also do not assert the exact PostgreSQL shutdown
log lines in the manual H3 checklist. These remain separate acceptance evidence.

## Local harness validation

Install `requirements.txt`, nginx, jq and OpenSSL, then run from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=omnigent/tests \
  python -m unittest discover -s omnigent/tests -p 'test_haos_harness.py' -v
```

`NGINX_BINARY` can select a locally installed nginx executable. The tests exercise
artifact selection, source binding, fail-closed preflight, ownership checks,
sanitized failure output and a real TLS proxy against a small HTTP fixture.
They neither download HAOS nor launch a VM. The acceptance entry point itself
requires the GitHub Actions environment and is invoked only by the PR workflow.
