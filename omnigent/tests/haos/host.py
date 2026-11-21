"""Owned host resources and verified input downloads for one disposable CI guest."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
import zipfile

import httpx

HAOS_URL = "https://github.com/home-assistant/operating-system/releases/download/18.1/haos_ova-18.1.qcow2.xz"
HAOS_SHA256 = "60df08773901e1eac9b9cfe03d53e1d939e67b669c172c3a41037fb3cd295b9d"
BASELINE_COMMIT = "191577fb937b1c131117083908e2fb54f5991e15"
BASELINE_CONFIG_SHA256 = "719c4bf219e7d176c5cffe1f6514dacc2c2fc1f47682a0a7b48b6ead565b5ade"
BASELINE_IMAGE = "ghcr.io/woowtech/woow-ha-omnigent-amd64@sha256:86d316c08de48166c942823362cef86f9c3e8680417b005a9ecead1b8921aa3e"
REGISTRY_IMAGE = "registry:2@sha256:a3d8aaa63ed8681a604f1dea0aa03f100d5895b6a58ace528858a7b332415373"
IMAGE_TAG = "upgrade-comparison/woow-omnigent:new"
PORTS = (10080, 18123, 18000, 12222, 15000, 18443)
GIB = 1024**3


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def wait_for(check, seconds, description, interval=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (httpx.HTTPError, OSError, ValueError, RuntimeError):
            pass
        time.sleep(interval)
    # Do not include HTTP bodies, process output, request URLs or credentials.
    raise RuntimeError("timeout: " + description)


def download(url, target, expected=None, *, limit=2 * GIB):
    """Never forward GitHub authentication to signed artifact download hosts."""
    digest, count = hashlib.sha256(), 0
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as response:
        require(response.status_code == 200, "download HTTP failure")
        with Path(target).open("wb") as output:
            for chunk in response.iter_bytes():
                count += len(chunk)
                require(count <= limit, "download exceeded size bound")
                digest.update(chunk)
                output.write(chunk)
    require(not expected or digest.hexdigest() == expected, "download checksum mismatch")
    return digest.hexdigest()


def extract_image(archive, target):
    with zipfile.ZipFile(archive) as z:
        require(z.namelist() == ["woow-omnigent.tar"], "unexpected artifact members")
        info = z.getinfo("woow-omnigent.tar")
        require(0 < info.file_size <= 2 * GIB, "image archive exceeded size bound")
        with z.open(info) as source, Path(target).open("wb") as output:
            shutil.copyfileobj(source, output)
    # Docker consumes the archive; reject unsafe paths and unexpected tags first.
    with tarfile.open(target) as t:
        members = t.getmembers()
        require(all(not m.name.startswith("/") and ".." not in Path(m.name).parts
                    and (m.isfile() or m.isdir()) for m in members), "unsafe image tar")
        manifest = json.load(t.extractfile("manifest.json"))
        require(len(manifest) == 1 and manifest[0]["RepoTags"] == [IMAGE_TAG],
                "unexpected image tag")
    return sha256(target)


def select_artifact(run, jobs, artifacts, head):
    """Only the successful amd64 build in this run; never guess across heads."""
    require(run["head_sha"] == head and run["event"] == "pull_request", "run identity mismatch")
    build = [j for j in jobs if j["name"] == "build (amd64)"]
    if len(build) != 1 or build[0]["conclusion"] != "success":
        return None
    # On a failed-jobs rerun GitHub retains the original job's run_attempt.
    attempt = build[0].get("run_attempt")
    require(isinstance(attempt, int) and attempt > 0, "build attempt missing")
    pair = []
    for wanted in (f"upgrade-comparison-image-woow-omnigent-attempt-{attempt}",
                   f"haos-image-source-attempt-{attempt}"):
        matches = [a for a in artifacts if a["name"] == wanted and not a["expired"]]
        require(len(matches) <= 1, "ambiguous candidate artifacts")
        if not matches:
            return None
        pair.append(matches[0])
    return pair


def candidate_artifact(private):
    repo, head = os.environ["GITHUB_REPOSITORY"], os.environ["HAOS_HEAD_SHA"]
    require(repo == "WOOWTECH/Woow_ha_omnigent_addon", "unexpected repository")
    require(re.fullmatch("[0-9a-f]{40}", head), "invalid source commit")
    # Read-only, short-lived job token is only used for these GitHub API calls.
    token = os.environ.pop("GH_TOKEN")
    with httpx.Client(base_url="https://api.github.com", timeout=30,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json"}) as api:
        def get(path, **params):
            r = api.get(f"/repos/{repo}" + path, params=params)
            require(r.status_code == 200, "GitHub read-only API failed")
            return r.json()

        def available():
            runs = get("/actions/workflows/publish-omnigent-addon-images.yml/runs",
                       head_sha=head, event="pull_request", per_page=20)["workflow_runs"]
            if not runs:
                return None
            run = max(runs, key=lambda r: r["id"])
            jobs = get(f"/actions/runs/{run['id']}/jobs", per_page=100)["jobs"]
            artifacts = get(f"/actions/runs/{run['id']}/artifacts", per_page=100)["artifacts"]
            artifact = select_artifact(run, jobs, artifacts, head)
            return (run, artifact, jobs) if artifact else None

        run, pair, jobs = wait_for(available, 900, "matching PR amd64 build artifact", 10)
        for artifact, filename in zip(pair, ("candidate.zip", "source.zip")):
            digest = artifact.get("digest", "")
            require(re.fullmatch("sha256:[0-9a-f]{64}", digest), "artifact checksum missing")
            r = api.get(f"/repos/{repo}/actions/artifacts/{artifact['id']}/zip")
            require(r.status_code in (302, 307), "artifact redirect missing")
            signed_url = r.headers["Location"]
            require(signed_url.startswith("https://"), "insecure artifact redirect")
            download(signed_url, private / filename, digest.removeprefix("sha256:"),
                     limit=2 * GIB if filename == "candidate.zip" else 64 * 1024)
    del token
    with zipfile.ZipFile(private / "source.zip") as z:
        require(z.namelist() == ["producer-source.json"], "unexpected source receipt members")
        require(z.getinfo("producer-source.json").file_size <= 16384, "source receipt too large")
        source = z.read("producer-source.json")
    receipt = json.loads(source)
    attempt = next(j["run_attempt"] for j in jobs if j["name"] == "build (amd64)")
    require(receipt["run_id"] == run["id"] and receipt["run_attempt"] == attempt,
            "source receipt run mismatch")
    (private / "producer-source.json").write_bytes(source)
    (private / "source.zip").unlink()
    archive = private / "candidate.zip"
    target = private / "candidate.tar"
    tar_digest = extract_image(archive, target)
    archive.unlink()
    return {"run_id": run["id"], "run_attempt": attempt, "artifact_id": pair[0]["id"],
            "head_sha": head, "artifact_digest": pair[0]["digest"], "tar_sha256": tar_digest,
            "source_artifact_id": pair[1]["id"], "source_artifact_digest": pair[1]["digest"]}


class Host:
    def __init__(self, private):
        self.private = Path(private)
        self.owner = "haos-ci-" + self.private.name
        self.processes = []
        self.registry_created = False
        self.guard_stop = threading.Event()
        self.disk_exhausted = False
        self.ssh_base = ["ssh", "-i", str(self.private / "key"), "-p", "12222",
                         "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                         "-o", "StrictHostKeyChecking=accept-new",
                         "-o", "UserKnownHostsFile=" + str(self.private / "known_hosts"),
                         "root@127.0.0.1"]
        self.qemu = None

    def run(self, argv, *, data=None, timeout=120, check=True):
        r = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
        # Private diagnostics are deliberately not an upload artifact.
        with (self.private / "commands.log").open("ab") as log:
            log.write(r.stdout + r.stderr + b"\n")
        require(not check or r.returncode == 0, "host/guest command failed")
        return r

    def start(self, argv):
        with (self.private / "processes.log").open("ab") as log:
            p = subprocess.Popen(argv, stdout=log, stderr=log)
        self.processes.append(p)
        return p

    def ssh(self, command, *, data=None, timeout=120, check=True):
        return self.run([*self.ssh_base, command], data=data, timeout=timeout, check=check)

    def supervisor(self, method, path, body=None, timeout=300):
        require(method in ("GET", "POST", "DELETE") and re.fullmatch(r"/[a-zA-Z0-9_./-]+", path),
                "invalid Supervisor request")
        # Root developer access belongs solely to this newly created guest.
        # The Supervisor token stays inside the guest shell and is never printed.
        command = r"""
set -eu
token=$(docker inspect hassio_cli | jq -r '.[0].Config.Env[] | select(startswith("SUPERVISOR_TOKEN=")) | sub("^SUPERVISOR_TOKEN=";"")')
test -n "$token"
address=$(docker inspect hassio_supervisor | jq -r '.[0].NetworkSettings.Networks | to_entries[0].value.IPAddress')
test -n "$address"
curl --silent --show-error --fail --max-time """ + str(timeout - 10) + r""" \
 -H "Authorization: Bearer $token" -H 'Content-Type: application/json' \
 -X """ + method + (" --data-binary @- " if body is not None else " ") + \
            '"http://$address' + path + '"'
        r = self.ssh(command, data=json.dumps(body).encode() if body is not None else None,
                     timeout=timeout)
        result = json.loads(r.stdout)
        require(result.get("result") == "ok", "Supervisor rejected operation")
        return result.get("data", {})

    def preflight(self):
        require(os.environ.get("GITHUB_ACTIONS") == "true", "real VM runner is CI-only")
        require(platform.machine() == "x86_64", "native amd64 required")
        # Test the KVM ioctl, not just the existence of a device node.
        with open("/dev/kvm", "r+b", buffering=0) as kvm:
            require(fcntl.ioctl(kvm, 0xAE00, 0) == 12, "unsupported KVM API")
            vm = fcntl.ioctl(kvm, 0xAE01, 0)
            os.close(vm)
        free = shutil.disk_usage(self.private).free
        require(free >= 12 * GIB, "at least 12 GiB physical free space required")
        for port in PORTS:
            with socket.socket() as s:
                s.bind(("127.0.0.1", port))
        return {"architecture": "amd64", "kvm_api": 12, "free_bytes": free,
                "guest_vcpus": 2, "guest_memory_mib": 6144, "virtual_disk_gib": 32}

    def watch_disk(self):
        def guard():
            while not self.guard_stop.wait(5):
                if shutil.disk_usage(self.private).free < 2 * GIB:
                    self.disk_exhausted = True
                    os.kill(os.getpid(), 15)
                    return
        threading.Thread(target=guard, daemon=True).start()

    def disk_guard(self):
        require(shutil.disk_usage(self.private).free >= 2 * GIB, "host disk reserve below 2 GiB")

    def prepare_vm(self):
        archive = self.private / "haos.qcow2.xz"
        download(HAOS_URL, archive, HAOS_SHA256)
        self.run(["xz", "-d", str(archive)], timeout=180)
        self.run(["qemu-img", "resize", str(self.private / "haos.qcow2"), "32G"])
        self.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(self.private / "key")])
        self.run(["truncate", "-s", "4M", str(self.private / "config.img")])
        self.run(["mkfs.vfat", "-n", "CONFIG", str(self.private / "config.img")])
        self.run(["mcopy", "-i", str(self.private / "config.img"),
                  str(self.private / "key.pub"), "::/authorized_keys"])
        shutil.copyfile("/usr/share/OVMF/OVMF_VARS.fd", self.private / "OVMF_VARS.fd")

    def boot(self):
        self.disk_guard()
        require(self.qemu is None or self.qemu.poll() is not None, "guest already running")
        self.qemu = self.start([
            "qemu-system-x86_64", "-machine", "q35,accel=kvm", "-cpu", "host",
            "-smp", "2", "-m", "6144", "-display", "none", "-monitor", "none",
            "-serial", "file:" + str(self.private / "console.log"),
            "-drive", "if=pflash,format=raw,readonly=on,file=/usr/share/OVMF/OVMF_CODE.fd",
            "-drive", "if=pflash,format=raw,file=" + str(self.private / "OVMF_VARS.fd"),
            "-drive", "if=virtio,format=qcow2,discard=unmap,file=" + str(self.private / "haos.qcow2"),
            "-device", "qemu-xhci,id=xhci", "-drive",
            "if=none,id=config,format=raw,readonly=on,file=" + str(self.private / "config.img"),
            "-device", "usb-storage,drive=config",
            "-nic", "user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:10080-:80,"
            "hostfwd=tcp:127.0.0.1:18123-:8123,hostfwd=tcp:127.0.0.1:18000-:8000,"
            "hostfwd=tcp:127.0.0.1:12222-:22222",
        ])
        wait_for(lambda: self.qemu.poll() is None and
                 self.ssh("true", check=False, timeout=10).returncode == 0,
                 600, "fresh HAOS developer SSH")
        def ready():
            info = self.supervisor("GET", "/supervisor/info", timeout=30)
            # Health belongs to supervisor/info; system state belongs to /info.
            root = self.supervisor("GET", "/info", timeout=30)
            if info.get("healthy") is True and root.get("state") == "running":
                return dict(info, state=root["state"])
            return None
        info = wait_for(ready, 600, "actual healthy Supervisor API")
        self.disk_guard()
        return {k: info.get(k) for k in ("version", "arch", "healthy", "state")}

    def registry(self):
        self.run(["docker", "run", "-d", "--name", self.owner,
                  "--label", "io.woow.haos-ci=" + self.owner,
                  "-p", "127.0.0.1:15000:5000", REGISTRY_IMAGE], timeout=180)
        self.registry_created = True
        self.registry_tunnel()

    def registry_tunnel(self):
        # Guest Docker recognizes loopback as a local registry. No insecure
        # registry configuration or extra container is added to HAOS.
        self.start([*self.ssh_base[:-1], "-N", "-o", "ExitOnForwardFailure=yes",
                    "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                    "-R", "127.0.0.1:15000:127.0.0.1:15000", self.ssh_base[-1]])
        wait_for(lambda: self.ssh("curl -fsS http://127.0.0.1:15000/v2/",
                                 check=False, timeout=10).returncode == 0,
                 30, "guest registry forwarding")

    def publish_local(self, source, version):
        target = "127.0.0.1:15000/omnigent-acceptance:" + version
        self.run(["docker", "tag", source, target])
        self.run(["docker", "push", target], timeout=240)
        result = self.run(["docker", "image", "inspect", source])
        image = json.loads(result.stdout)[0]
        require(image["Architecture"] == "amd64", "wrong add-on architecture")
        require(image["Config"]["Labels"].get("io.hass.version") == version, "wrong add-on version")
        return image

    def install_config(self, config):
        import yaml
        require(config["slug"] == "woow-omnigent", "unexpected add-on slug")
        config = dict(config, image="127.0.0.1:15000/omnigent-acceptance")
        # Local and store installs have different slugs. Both sides use local.
        self.ssh("mkdir -p /mnt/data/supervisor/addons/local/woow-omnigent && "
                 "cat > /mnt/data/supervisor/addons/local/woow-omnigent/config.yaml",
                 data=yaml.safe_dump(config).encode())
        self.supervisor("POST", "/store/reload", {})

    def verify_guest_image(self, expected_id):
        # Names changed from addon_ to app_ in recent Supervisor releases.
        command = "docker inspect app_local_woow-omnigent 2>/dev/null || docker inspect addon_local_woow-omnigent"
        image = json.loads(self.ssh(command).stdout)[0]
        require(image["Image"] == expected_id, "Supervisor is not running the verified image")
        return image["Name"].lstrip("/")

    def cold_shutdown(self):
        # Clean HAOS shutdown; preserve the disk but not the running VM process.
        self.ssh("systemctl poweroff", check=False, timeout=20)
        self.qemu.wait(timeout=90)
        require(self.qemu.returncode == 0, "HAOS did not shut down cleanly")

    def cleanup(self):
        self.guard_stop.set()
        for p in reversed(self.processes):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=2)
        if self.registry_created:
            r = self.run(["docker", "inspect", "--format",
                          '{{index .Config.Labels "io.woow.haos-ci"}}', self.owner],
                         check=False, timeout=5)
            require(r.stdout.decode().strip() == self.owner, "registry ownership mismatch")
            self.run(["docker", "rm", "-f", "-v", self.owner], timeout=10)
