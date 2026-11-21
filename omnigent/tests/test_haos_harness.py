"""Harness regression tests. These are not HAOS/Supervisor acceptance evidence."""
import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import hashlib
import json
import os
from pathlib import Path
import socket
import tarfile
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, mock_open, patch
import zipfile

import httpx
import yaml

from haos import acceptance, host, provenance


HEAD = "a" * 40
TREE = "b" * 40


def registry_fixture(version="0.1.24", *, revision="pushed"):
    config = json.dumps({"architecture": "amd64", "os": "linux", "config": {"Labels": {
        "io.hass.version": version}}}).encode()
    config_id = "sha256:" + hashlib.sha256(config).hexdigest()
    manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"digest": config_id, "size": len(config), "mediaType": "application/vnd.oci.image.config.v1+json"},
        "layers": [], "annotations": {"fixture": revision}}).encode()
    return config, config_id, manifest, "sha256:" + hashlib.sha256(manifest).hexdigest()


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.run = {"head_sha": HEAD, "event": "pull_request", "run_attempt": 2}
        self.jobs = [{"name": "build (amd64)", "conclusion": "success", "run_attempt": 1}]
        self.artifacts = [
            {"name": "upgrade-comparison-image-woow-omnigent-attempt-1", "expired": False},
            {"name": "haos-image-source-attempt-1", "expired": False},
        ]

    def select(self):
        return host.select_artifact(self.run, self.jobs, self.artifacts, HEAD)

    def test_uses_build_attempt_when_other_job_reruns(self):
        self.assertEqual(self.select(), self.artifacts)

    def test_rebuilt_job_cannot_reuse_old_artifact(self):
        self.jobs[0]["run_attempt"] = 2
        self.assertIsNone(self.select())

    def test_wrong_head_rejected(self):
        self.run["head_sha"] = "c" * 40
        with self.assertRaises(RuntimeError):
            self.select()

    def test_non_pr_rejected(self):
        self.run["event"] = "push"
        with self.assertRaises(RuntimeError):
            self.select()

    def test_failed_build_not_accepted(self):
        self.jobs[0]["conclusion"] = "failure"
        self.assertIsNone(self.select())

    def test_expired_image_not_accepted(self):
        self.artifacts[0]["expired"] = True
        self.assertIsNone(self.select())

    def test_missing_source_receipt_not_accepted(self):
        self.artifacts.pop()
        self.assertIsNone(self.select())

    def test_ambiguous_image_rejected(self):
        self.artifacts.append(copy.deepcopy(self.artifacts[0]))
        with self.assertRaises(RuntimeError):
            self.select()

    def test_unknown_attempt_rejected(self):
        del self.jobs[0]["run_attempt"]
        with self.assertRaises(RuntimeError):
            self.select()

    def make_archive(self, directory, *, member="woow-omnigent.tar", tag=host.IMAGE_TAG,
                     extra=None):
        blob = io.BytesIO()
        with tarfile.open(fileobj=blob, mode="w") as t:
            content = json.dumps([{"RepoTags": [tag]}]).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(content)
            t.addfile(info, io.BytesIO(content))
            if extra:
                t.addfile(tarfile.TarInfo(extra))
        archive = Path(directory) / "image.zip"
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr(member, blob.getvalue())
        return archive

    def test_valid_archive(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "image.tar"
            digest = host.extract_image(self.make_archive(d), target)
            self.assertEqual(digest, host.sha256(target))

    def test_wrong_archive_member(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
            host.extract_image(self.make_archive(d, member="../image.tar"), Path(d) / "image.tar")

    def test_wrong_image_tag(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
            host.extract_image(self.make_archive(d, tag="other:latest"), Path(d) / "image.tar")

    def test_tar_path_traversal(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
            host.extract_image(self.make_archive(d, extra="../../outside"), Path(d) / "image.tar")


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.receipt = {"schema": 1, "pr_head": HEAD, "checkout_commit": "c" * 40,
                        "addon_tree": TREE, "image_id": "sha256:" + "d" * 64,
                        "files": provenance.hashes(acceptance.ROOT)}

    def validate(self):
        with patch.dict(os.environ, {"HAOS_HEAD_SHA": HEAD}):
            provenance.validate_source(self.receipt, HEAD, TREE, acceptance.ROOT)

    def test_merge_checkout_with_identical_addon_tree(self):
        self.validate()

    def test_merge_checkout_with_changed_addon_tree_rejected(self):
        self.receipt["addon_tree"] = "e" * 40
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_changed_pin_rejected(self):
        self.receipt["files"]["build/artifact-pin.json"] = "0" * 64
        with self.assertRaises(RuntimeError):
            self.validate()

    def test_wrong_acceptance_checkout_rejected(self):
        with patch.dict(os.environ, {"HAOS_HEAD_SHA": HEAD}), self.assertRaises(RuntimeError):
            provenance.validate_source(self.receipt, "f" * 40, TREE, acceptance.ROOT)

    def test_unvalidated_image_identity_rejected(self):
        self.receipt["image_id"] = "not-a-digest"
        with self.assertRaises(RuntimeError):
            self.validate()


class ArchiveIdentityTests(unittest.TestCase):
    def archive(self, directory, *, config_data=None, config_reference=None, tag=host.IMAGE_TAG,
                duplicate=False, bad_manifest_hash=False, bad_descriptor_size=False):
        config = config_data or b'{"architecture":"amd64","os":"linux","rootfs":{"type":"layers","diff_ids":[]}}'
        config_id = "sha256:" + hashlib.sha256(config).hexdigest()
        config_name = "blobs/sha256/" + config_id[7:]
        manifest = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_reference or config_id, "size": len(config)}, "layers": []}).encode()
        manifest_id = "sha256:" + hashlib.sha256(manifest).hexdigest()
        files = {"manifest.json": json.dumps([{"Config": config_name, "RepoTags": [tag], "Layers": []}]).encode(),
                 config_name: config,
                 "blobs/sha256/" + manifest_id[7:]: manifest + (b" " if bad_manifest_hash else b""),
                 "index.json": json.dumps({"schemaVersion": 2, "manifests": [{"digest": manifest_id,
                     "size": len(manifest) + int(bad_descriptor_size),
                     "mediaType": "application/vnd.oci.image.manifest.v1+json"}]}).encode()}
        target = Path(directory) / "candidate.tar"
        with tarfile.open(target, "w") as t:
            for name, data in list(files.items()) + ([('manifest.json', files['manifest.json'])] if duplicate else []):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return target, config_id, manifest_id

    def test_manifest_producer_and_config_consumer_are_cryptographically_linked(self):
        with tempfile.TemporaryDirectory() as d:
            path, config_id, manifest_id = self.archive(d)
            self.assertNotEqual(config_id, manifest_id)
            identity = provenance.archive_identity(path, manifest_id)
            self.assertEqual(identity, {"config_digest": config_id, "producer_image_id": manifest_id,
                                        "producer_id_kind": "manifest"})

    def test_legacy_config_producer_identity(self):
        with tempfile.TemporaryDirectory() as d:
            path, config_id, _ = self.archive(d)
            self.assertEqual(provenance.archive_identity(path, config_id)["producer_id_kind"], "config")

    def test_unknown_producer_id_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path, _, _ = self.archive(d)
            with self.assertRaises(RuntimeError):
                provenance.archive_identity(path, "sha256:" + "0" * 64)

    def test_changed_config_reference_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path, _, manifest_id = self.archive(d, config_reference="sha256:" + "0" * 64)
            with self.assertRaises(RuntimeError):
                provenance.archive_identity(path, manifest_id)

    def test_corrupt_manifest_duplicate_member_and_wrong_tag_rejected(self):
        for change in ({"bad_manifest_hash": True}, {"duplicate": True}, {"tag": "other:new"},
                       {"bad_descriptor_size": True}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as d:
                path, _, manifest_id = self.archive(d, **change)
                with self.assertRaises(RuntimeError):
                    provenance.archive_identity(path, manifest_id)


class HostTests(unittest.TestCase):
    def test_baseline_anchor_exports_only_pulled_immutable_image_and_deletes_tar(self):
        for mismatch in (False, True):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                tag = "haos-acceptance-baseline:0.1.23"
                fixture, config_id, _ = ArchiveIdentityTests().archive(d, tag=tag)
                def command(argv, **kwargs):
                    if argv[:3] == ["docker", "image", "inspect"]:
                        self.assertEqual(argv[-1], host.BASELINE_IMAGE)
                        value = "sha256:" + "0"*64 if mismatch else config_id
                        return SimpleNamespace(returncode=0, stdout=value.encode(), stderr=b"")
                    if argv[1] == "tag":
                        self.assertEqual(argv, ["docker", "tag", host.BASELINE_IMAGE, tag])
                    if argv[1] == "save":
                        self.assertEqual(argv[-1], tag)
                        Path(argv[3]).write_bytes(fixture.read_bytes())
                    return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                h.run = Mock(side_effect=command)
                if mismatch:
                    with self.assertRaises(RuntimeError):
                        h.baseline_identity()
                else:
                    self.assertEqual(h.baseline_identity()["config_digest"], config_id)
                self.assertFalse((Path(d) / "baseline-image.tar").exists())

    def test_registry_metadata_is_bounded_loopback_only_and_does_not_redirect(self):
        for status, body in ((200, b"{}"), (302, b""), (200, b"x"*(1024*1024+1))):
            with self.subTest(status=status, size=len(body)), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                def respond(request):
                    self.assertEqual(str(request.url), "http://127.0.0.1:15000/v2/omnigent-acceptance/manifests/0.1.24")
                    return httpx.Response(status, content=body, headers={
                        "Docker-Content-Digest": "sha256:" + "d"*64, "Location": "https://invalid.example"})
                with httpx.Client(transport=httpx.MockTransport(respond), trust_env=False) as client:
                    def stream(method, url, **kwargs):
                        self.assertFalse(kwargs.pop("trust_env"))
                        self.assertFalse(kwargs["follow_redirects"])
                        return client.stream(method, url, **kwargs)
                    with patch("haos.host.httpx.stream", side_effect=stream):
                        if status == 200 and len(body) <= 1024*1024:
                            self.assertEqual(h._registry_metadata("manifests/0.1.24")[0], body)
                        else:
                            with self.assertRaises(RuntimeError):
                                h._registry_metadata("manifests/0.1.24")
                self.assertNotIn("invalid.example", json.dumps(h.registry_progress))
                with self.assertRaises(RuntimeError):
                    h._registry_metadata("https://invalid.example/private")

    def test_pushed_manifest_binding_handles_host_config_guest_manifest_for_both_versions(self):
        for version in ("0.1.23", "0.1.24"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                config, config_id, manifest, pushed_id = registry_fixture(version)
                _, _, _, producer_id = registry_fixture(version, revision="producer")
                self.assertNotEqual(producer_id, pushed_id)
                anchor = {"config_digest": config_id, "producer_image_id": producer_id}
                image = {"Id": config_id, "Architecture": "amd64", "Config": {
                    "Labels": {"io.hass.version": version}}}
                h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=b"", stderr=b"")]*2 + [
                    SimpleNamespace(returncode=0, stdout=json.dumps([image]).encode(), stderr=b"")])
                h._registry_metadata = Mock(side_effect=[(manifest, pushed_id), (config, config_id)])
                source = host.BASELINE_IMAGE if version == "0.1.23" else host.IMAGE_TAG
                h.publish_local(source, version, identity=anchor)
                for guest_id in (pushed_id, config_id):
                    h.ssh = Mock(return_value=SimpleNamespace(stdout=json.dumps([
                        {"Image": guest_id, "Name": "/app_local_woow-omnigent"}]).encode()))
                    self.assertEqual(h.verify_guest_image(config_id), "app_local_woow-omnigent")
                for wrong in (producer_id, "sha256:" + "f" * 64):
                    h.ssh.return_value.stdout = json.dumps([
                        {"Image": wrong, "Name": "/app_local_woow-omnigent"}]).encode()
                    with self.assertRaises(RuntimeError):
                        h.verify_guest_image(config_id)

    def test_guest_identity_without_registry_proof_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.ssh = Mock(return_value=SimpleNamespace(stdout=b'[{"Image":"same","Name":"/app_local_woow-omnigent"}]'))
            with self.assertRaises(RuntimeError):
                h.verify_guest_image("same")

    def test_registry_manifest_wrong_hash_config_and_anchor_rejected(self):
        config, config_id, manifest, pushed_id = registry_fixture()
        for raw, digest, blob, expected in ((manifest+b" ", pushed_id, config, config_id),
                (manifest, "sha256:"+"0"*64, config, config_id),
                (manifest, pushed_id, config+b" ", config_id),
                (manifest, pushed_id, config, "sha256:"+"0"*64)):
            with self.subTest(digest=digest, expected=expected), self.assertRaises(RuntimeError):
                provenance.registry_identity(raw, digest, blob, expected)

    def test_registry_failure_retains_command_exit_without_raw_output(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            with patch("haos.host.subprocess.run", side_effect=[
                    SimpleNamespace(returncode=0, stdout=b"", stderr=b""), SimpleNamespace(
                    returncode=125, stdout=b"", stderr=b"denied synthetic-secret /mcp/private_token")]):
                with self.assertRaises(RuntimeError):
                    h.registry()
            self.assertEqual(h.registry_progress["stage"], "registry_launch")
            self.assertEqual(h.registry_progress["commands"][-1], {
                "stage": "registry_launch", "exit": 125, "timeout": False,
                "error_codes": ["access_denied"]})
            self.assertIsNone(h.registry_created)
            self.assertEqual(h.registry_progress["exception"], "RuntimeError")
            self.assertEqual(h.registry_progress["states"], {"registry_pull": True, "registry_launch": False})
            self.assertNotIn("synthetic-secret", json.dumps(h.registry_progress))
            self.assertNotIn("private_token", json.dumps(h.registry_progress))

    def test_registry_push_timeout_retains_exact_image_stage(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=b"", stderr=b""),
                host.subprocess.TimeoutExpired(["docker", "synthetic-secret"], 240)])
            with self.assertRaises(host.subprocess.TimeoutExpired):
                h.publish_local(host.IMAGE_TAG, "0.1.24", identity={"config_digest": "sha256:" + "c"*64, "producer_image_id": "sha256:" + "c"*64})
            self.assertEqual(h.registry_progress["stage"], "candidate_push")
            self.assertEqual(h.registry_progress["commands"][-1], {
                "stage": "candidate_push", "exit": None, "timeout": True, "error_codes": []})
            self.assertNotIn("synthetic-secret", json.dumps(h.registry_progress))

    def test_registry_forwarding_failure_retains_probe_and_tunnel_exit(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.start = Mock(return_value=Mock(poll=Mock(return_value=255)))
            h.ssh = Mock(return_value=SimpleNamespace(returncode=7))
            def once(check, *args):
                self.assertFalse(check())
                raise RuntimeError("timeout")
            with patch("haos.host.wait_for", side_effect=once), self.assertRaises(RuntimeError):
                h.registry_tunnel()
            self.assertEqual(h.registry_progress["stage"], "guest_registry_forwarding")
            self.assertEqual(h.registry_progress["forwarding"], {
                "probe_exit": 7, "probe_timeout": False, "tunnel_exit": 255})

    def test_registry_image_version_guard_remains_enforced_and_identified(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            image = {"Architecture": "amd64", "Config": {"Labels": {"io.hass.version": "wrong"}}}
            h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=b"", stderr=b"")]*2 + [
                SimpleNamespace(returncode=0, stdout=json.dumps([image]).encode(), stderr=b"")])
            with self.assertRaisesRegex(RuntimeError, "wrong add-on version"):
                h.publish_local(host.BASELINE_IMAGE, "0.1.23", identity={"config_digest": "sha256:" + "c"*64, "producer_image_id": "sha256:" + "c"*64})
            self.assertEqual(h.registry_progress["stage"], "baseline_version")
            self.assertTrue(all(c["exit"] == 0 for c in h.registry_progress["commands"]))

    def test_registry_all_command_boundaries_preserve_failures_and_markers(self):
        stages = ["registry_pull", "registry_launch", "registry_inspect"] + [
            role + "_" + step for role in ("baseline", "candidate") for step in ("tag", "push", "inspect")]
        for stage in stages:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.run = Mock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=
                    b"x509: https://synthetic-secret no such file or directory"))
                with self.assertRaises(RuntimeError):
                    h._registry_run(stage, ["docker", "synthetic-secret"])
                self.assertEqual(h.registry_progress["stage"], stage)
                self.assertFalse(h.registry_progress["states"][stage])
                self.assertEqual(h.registry_progress["commands"][0]["error_codes"],
                                 ["certificate_error", "missing_path"])
                self.assertNotIn("synthetic-secret", json.dumps(h.registry_progress))

    def test_registry_exception_class_is_allowlisted_without_details(self):
        class SecretException(Exception):
            pass
        for error, kind in ((OSError("private-path"), "OSError"),
                            (ValueError("private-value"), "ValueError"),
                            (SecretException("private-value"), "other")):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.run = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    h._registry_run("registry_pull", ["docker", "private-command"])
                self.assertEqual(h.registry_progress["exception"], kind)
                self.assertNotIn("private-", json.dumps(h.registry_progress))
                self.assertNotIn("SecretException", json.dumps(h.registry_progress))

    def test_registry_container_and_host_http_boundaries(self):
        for running, status in ((False, None), (True, 503), (True, 200)):
            with self.subTest(running=running, status=status), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=b"", stderr=b"")]*2 + [
                    SimpleNamespace(returncode=0, stdout=json.dumps(
                        {"Running": running, "ExitCode": 0 if running else 2}).encode(), stderr=b"")])
                h.registry_tunnel = Mock()
                def once(check, *args):
                    host.require(check(), "timeout")
                with patch("haos.host.wait_for", side_effect=once), \
                        patch("haos.host.httpx.get", return_value=SimpleNamespace(status_code=status)):
                    if running and status == 200:
                        h.registry()
                        h.registry_tunnel.assert_called_once()
                        self.assertTrue(h.registry_progress["states"]["host_registry_http"])
                    else:
                        with self.assertRaises(RuntimeError):
                            h.registry()
                        h.registry_tunnel.assert_not_called()
                        stage = "host_registry_http" if running else "registry_container_state"
                        self.assertEqual(h.registry_progress["stage"], stage)
                        self.assertFalse(h.registry_progress["states"][stage])
                self.assertTrue(h.registry_created)
                self.assertIs(h.registry_progress["container_running"], running)
                commands = [call.args[0] for call in h.run.call_args_list]
                self.assertEqual(commands[0], ["docker", "pull", "--platform", "linux/amd64", host.REGISTRY_IMAGE])
                self.assertIn("--pull=never", commands[1])
                self.assertIn("127.0.0.1:15000:5000", commands[1])

    def test_registry_image_metadata_and_architecture_guards(self):
        for payload, stage in ((b"invalid", "candidate_metadata"),
                               (json.dumps([{"Architecture": "arm64"}]).encode(), "candidate_architecture")):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=b"", stderr=b"")]*2 + [
                    SimpleNamespace(returncode=0, stdout=payload, stderr=b"")])
                with self.assertRaises((ValueError, RuntimeError)):
                    h.publish_local(host.IMAGE_TAG, "0.1.24", identity={"config_digest": "sha256:" + "c"*64, "producer_image_id": "sha256:" + "c"*64})
                self.assertEqual(h.registry_progress["stage"], stage)
                self.assertFalse(h.registry_progress["states"][stage])

    def test_registry_host_http_exception_remains_safe_and_retryable(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=b"", stderr=b"")]*2 + [
                SimpleNamespace(returncode=0, stdout=b'{"Running":true,"ExitCode":0}', stderr=b"")])
            def once(check, *args):
                self.assertFalse(check())
                raise RuntimeError("timeout")
            with patch("haos.host.wait_for", side_effect=once), \
                    patch("haos.host.httpx.get", side_effect=httpx.ConnectError("private-url")), \
                    self.assertRaises(RuntimeError):
                h.registry()
            self.assertEqual(h.registry_progress["stage"], "host_registry_http")
            self.assertEqual(h.registry_progress["host_http"], {"http_status": None, "exception": "HTTPError"})
            self.assertNotIn("private-url", json.dumps(h.registry_progress))

    def test_registry_forward_timeout_and_start_failure_remain_distinct(self):
        for start_fails in (True, False):
            with self.subTest(start_fails=start_fails), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.start = Mock(side_effect=OSError("private-path")) if start_fails else Mock(
                    return_value=Mock(poll=Mock(return_value=None)))
                h.ssh = Mock(side_effect=host.subprocess.TimeoutExpired(["private-command"], 10))
                with patch("haos.host.wait_for", side_effect=lambda check, *args: check()), \
                        self.assertRaises((OSError, host.subprocess.TimeoutExpired)):
                    h.registry_tunnel()
                self.assertEqual(h.registry_progress["stage"], "guest_registry_forwarding")
                self.assertEqual(h.registry_progress["exception"], "OSError" if start_fails else "TimeoutExpired")
                self.assertEqual(h.registry_progress["forwarding"]["probe_timeout"], not start_fails)
                self.assertNotIn("private-", json.dumps(h.registry_progress))

    def test_kvm_refresh_settles_before_each_grant_and_keeps_original_acl(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            identity = (1, 2, 3, 0, 100)
            snapshots = [(identity, b"original", False), (identity, b"grant-one", True),
                         (identity, b"original", False), (identity, b"grant-two", True)]
            h._kvm_access_snapshot = Mock(side_effect=snapshots)
            h.run = Mock()
            with patch("haos.host.os.getuid", return_value=1001):
                h.refresh_kvm_access("preflight")
                h.refresh_kvm_access("boot")
            self.assertEqual(h.original_kvm_acl, b"original")
            self.assertEqual(h.kvm_access_baseline, snapshots[-1])
            self.assertEqual([c.args[0] for c in h.run.call_args_list], [
                ["udevadm", "settle", "--timeout=5"],
                ["sudo", "-n", "setfacl", "-m", "u:1001:rw", "/dev/kvm"],
                ["udevadm", "settle", "--timeout=5"],
                ["sudo", "-n", "setfacl", "-m", "u:1001:rw", "/dev/kvm"]])
            self.assertTrue(h.kvm_access_progress["refreshes"][1]["acl_changed_since_grant"])
            self.assertFalse(h.kvm_access_progress["refreshes"][1]["effective_access_before"])
            self.assertTrue(h.kvm_access_progress["refreshes"][1]["effective_access_after"])
            self.assertNotIn("original", json.dumps(h.kvm_access_progress))
            self.assertNotIn("1001", json.dumps(h.kvm_access_progress))

    def test_kvm_refresh_settle_failure_prevents_grant(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.run = Mock(side_effect=RuntimeError("settle failed"))
            h._kvm_access_snapshot = Mock()
            with self.assertRaises(RuntimeError):
                h.refresh_kvm_access("boot")
            h.run.assert_called_once_with(["udevadm", "settle", "--timeout=5"], timeout=6)
            h._kvm_access_snapshot.assert_not_called()

    def test_kvm_ineffective_grant_fails_and_preserves_pre_grant_identity(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            identity = (1, 2, 3, 0, 100)
            h._kvm_access_snapshot = Mock(side_effect=[
                (identity, b"original", False), (identity, b"changed-acl", False)])
            def run(argv, **kwargs):
                if argv[0] == "sudo":
                    self.assertEqual(h.original_kvm_acl, b"original")
                    self.assertEqual(h.kvm_identity, identity)
            h.run = Mock(side_effect=run)
            with self.assertRaisesRegex(RuntimeError, "grant ineffective"):
                h.refresh_kvm_access("preflight")
            self.assertIsNone(h.kvm_access_baseline)
            self.assertFalse(h.kvm_access_progress["refreshes"][0]["effective_access_after"])

    def test_kvm_refresh_refuses_replaced_device(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.original_kvm_acl = b"original"
            h.kvm_identity = (1, 2, 3, 0, 100)
            h.kvm_access_baseline = (h.kvm_identity, b"grant", True)
            h._kvm_access_snapshot = Mock(return_value=((1, 9, 3, 0, 100), b"other", False))
            h.run = Mock()
            with self.assertRaisesRegex(RuntimeError, "KVM device identity"):
                h.refresh_kvm_access("boot")
            h.run.assert_called_once_with(["udevadm", "settle", "--timeout=5"], timeout=6)

    def test_kvm_cleanup_reaps_then_settles_then_restores_original_acl(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.original_kvm_acl = b"original-private-acl"
            h.kvm_identity = (1, 2, 3, 0, 100)
            h._kvm_access_snapshot = Mock(return_value=(h.kvm_identity, b"grant", True))
            events = []
            qemu = Mock()
            qemu.poll.return_value = None
            qemu.terminate.side_effect = lambda: events.append("terminate")
            def reaped(**kwargs):
                events.append("reap")
                qemu.poll.return_value = 0
            qemu.wait.side_effect = reaped
            h.qemu, h.processes = qemu, [qemu]
            def run(argv, **kwargs):
                events.append("settle" if argv[0] == "udevadm" else "restore")
                if argv[0] == "sudo":
                    self.assertEqual(kwargs["data"], b"original-private-acl")
            h.run = Mock(side_effect=run)
            h.cleanup()
            self.assertEqual(events, ["terminate", "reap", "settle", "restore"])
            self.assertTrue(h.kvm_access_progress["restored"])

    def test_kvm_cleanup_restores_even_when_settle_fails(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.original_kvm_acl = b"original"
            h.kvm_identity = (1, 2, 3, 0, 100)
            h._kvm_access_snapshot = Mock(return_value=(h.kvm_identity, b"grant", True))
            h.run = Mock(side_effect=[RuntimeError("settle failed"), SimpleNamespace(returncode=0)])
            with self.assertRaisesRegex(RuntimeError, "settle failed"):
                h.cleanup()
            self.assertEqual(h.run.call_args.args[0], ["sudo", "-n", "setfacl", "--restore=-"])
            self.assertEqual(h.run.call_args.kwargs["data"], b"original")
            self.assertTrue(h.kvm_access_progress["restored"])
            self.assertEqual(h.kvm_access_progress["cleanup_failure"], "udev_settle")

    def test_kvm_cleanup_refuses_replaced_device_or_owner(self):
        original = (1, 2, 3, 0, 100)
        for index in range(len(original)):
            changed = list(original)
            changed[index] += 1
            with self.subTest(index=index), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.original_kvm_acl = b"original"
                h.kvm_identity = original
                h._kvm_access_snapshot = Mock(return_value=(tuple(changed), b"other", False))
                h.run = Mock()
                with self.assertRaisesRegex(RuntimeError, "KVM device identity"):
                    h.cleanup()
                h.run.assert_called_once_with(["udevadm", "settle", "--timeout=5"], timeout=6)
                self.assertFalse(h.kvm_access_progress["restored"])
                self.assertEqual(h.kvm_access_progress["cleanup_failure"], "device_identity")

    def test_kvm_snapshot_ignores_acl_mode_bits_but_requires_character_device(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.run = Mock(return_value=SimpleNamespace(stdout=b"private-acl"))
            info = SimpleNamespace(st_dev=1, st_ino=2, st_rdev=3, st_uid=0, st_gid=100,
                                   st_mode=host.stat.S_IFCHR | 0o660)
            with patch("haos.host.Path.stat", return_value=info), patch("haos.host.os.access", return_value=True):
                first = h._kvm_access_snapshot()
                info.st_mode = host.stat.S_IFCHR | 0o600
                self.assertEqual(h._kvm_access_snapshot()[0], first[0])
                info.st_mode = host.stat.S_IFREG | 0o660
                h.original_kvm_acl, h.kvm_identity = first[1], first[0]
                h.run.reset_mock()
                with self.assertRaisesRegex(RuntimeError, "character device"):
                    h.cleanup()
                h.run.assert_called_once_with(["udevadm", "settle", "--timeout=5"], timeout=6)
                self.assertEqual(h.kvm_access_progress["cleanup_failure"], "device_identity")

    def test_kvm_refresh_immediately_precedes_initial_and_cold_boot(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            events = []
            h.disk_guard = Mock()
            h.refresh_kvm_access = Mock(side_effect=lambda phase: events.append("refresh"))
            qemu = Mock(poll=Mock(return_value=0))
            def start(argv, **kwargs):
                self.assertEqual(argv[0], "qemu-system-x86_64")
                self.assertIn("q35,accel=kvm", argv)
                self.assertNotIn("sudo", argv)
                events.append("start")
                return qemu
            h.start = Mock(side_effect=start)
            h._wait_boot = Mock(return_value={})
            h._capture_boot_signals = Mock()
            with contextlib.redirect_stdout(io.StringIO()):
                h.boot()
                h.boot()
            self.assertEqual(events, ["refresh", "start", "refresh", "start"])

    def test_exited_qemu_fails_immediately_instead_of_waiting_for_ssh(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.disk_guard = Mock()
            h.refresh_kvm_access = Mock()
            h.start = Mock(return_value=Mock(poll=Mock(return_value=1)))
            h.ssh = Mock()
            # One predicate call is enough to detect a dead QEMU process.
            def check_once(check, *args):
                result = check()
                self.assertTrue(result, "dead QEMU was treated as retryable SSH readiness")
                return result
            with patch("haos.host.wait_for", side_effect=check_once), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(host.BootFailure, "QEMU exited before guest readiness"):
                    h.boot()
            h.ssh.assert_not_called()
            self.assertEqual(h.boot_progress["qemu_exit"], 1)

    def test_terminal_boot_failure_is_not_swallowed_by_wait_for(self):
        check = Mock(side_effect=host.BootFailure("QEMU exited before guest readiness"))
        with patch("haos.host.time.sleep") as sleep, self.assertRaises(host.BootFailure):
            host.wait_for(check, 600, "fresh HAOS developer SSH")
        sleep.assert_not_called()
        check.assert_called_once()

    def test_boot_diagnostics_export_labels_not_raw_log_content(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.qemu = Mock(poll=Mock(return_value=1))
            secret = "private_omcp_SYNTHETIC_SECRET https://example.invalid/?token=secret"
            (Path(d) / "qemu.log").write_text("Block node is read-only\n" + secret)
            (Path(d) / "console.log").write_text("Linux version fixture\nUpdate SSH authorized_keys!\n" + secret)
            h._capture_boot_signals()
            self.assertEqual(h.boot_progress, {
                "qemu_exit": 1, "qemu_error_codes": ["readonly_block"],
                "console_markers": ["linux_started", "ssh_key_imported"]})
            self.assertNotIn(secret, json.dumps(h.boot_progress))

    def test_boot_ssh_diagnostics_export_error_class_only(self):
        for error, expected in (
            (b"Permission denied (publickey). private_omcp_SYNTHETIC", "authentication_denied"),
            (b"Connection refused private_omcp_SYNTHETIC", "connection_refused"),
            (b"unknown private_omcp_SYNTHETIC", "unclassified"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.qemu = Mock(poll=Mock(return_value=None))
                h.boot_progress["ssh_attempts"] = 0
                h.ssh = Mock(return_value=SimpleNamespace(returncode=255, stderr=error))
                self.assertFalse(h._ssh_ready())
                self.assertEqual(h.boot_progress["last_ssh_error"], expected)
                self.assertEqual(h.boot_progress["ssh_attempts"], 1)
                self.assertNotIn("SYNTHETIC", json.dumps(h.boot_progress))

    def test_boot_ssh_timeout_is_bounded_and_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.qemu = Mock(poll=Mock(return_value=None))
            h.boot_progress["ssh_attempts"] = 0
            h.boot_progress["last_ssh_exit"] = 255
            h.ssh = Mock(side_effect=host.subprocess.TimeoutExpired("private command", 10))
            self.assertFalse(h._ssh_ready())
            self.assertEqual(h.boot_progress["last_ssh_error"], "command_timeout")
            self.assertIsNone(h.boot_progress["last_ssh_exit"])

    def boot_fixture(self, supervisor_info, root_info):
        """Use the real response envelopes, parser and readiness predicate."""
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.disk_guard = Mock()
            h.refresh_kvm_access = Mock()
            h.start = Mock(return_value=Mock(poll=Mock(return_value=None)))
            visited = []
            def ssh(command, **kwargs):
                if command == "true":
                    return SimpleNamespace(returncode=0)
                for path, data in (("/supervisor/info", supervisor_info), ("/info", root_info)):
                    if '"http://$address' + path + '"' in command:
                        visited.append(path)
                        return SimpleNamespace(returncode=0, stderr=b"HAOS_BOOT curl_exit 0\n",
                                               stdout=json.dumps({"result": "ok", "data": data}).encode() + b"\n200\n")
                self.fail("unexpected guest command")
            h.ssh = Mock(side_effect=ssh)
            def check_once(check, seconds, description, interval=3):
                result = check()
                if not result:
                    raise RuntimeError("not ready")
                return result
            with patch("haos.host.wait_for", side_effect=check_once), contextlib.redirect_stdout(io.StringIO()):
                receipt = h.boot()
            self.assertEqual(visited, ["/supervisor/info", "/info"])
            return receipt

    def test_boot_accepts_actual_supervisor_and_root_info_shapes(self):
        # API supervisor.py has healthy/version and nested app states, but no
        # top-level state. API root.py exposes the system state at GET /info.
        supervisor_info = {"version": "2026.11.0", "arch": "amd64", "healthy": True,
                           "supported": True, "addons": [{"slug": "fixture", "state": "stopped"}]}
        root_info = {"supervisor": "2026.11.0", "hassos": "18.1", "arch": "amd64",
                     "state": "running", "supported": True}
        self.assertEqual(self.boot_fixture(supervisor_info, root_info),
                         {"version": "2026.11.0", "arch": "amd64", "healthy": True, "state": "running"})

    def test_boot_requires_healthy_supervisor_info(self):
        for supervisor_info in ({"healthy": False}, {}):
            with self.subTest(supervisor_info=supervisor_info), self.assertRaisesRegex(RuntimeError, "not ready"):
                self.boot_fixture(supervisor_info, {"state": "running"})

    def test_boot_requires_running_root_info(self):
        for root_info in ({"state": "startup"}, {"state": None}, {}):
            with self.subTest(root_info=root_info), self.assertRaisesRegex(RuntimeError, "not ready"):
                self.boot_fixture({"healthy": True, "addons": [{"state": "started"}]}, root_info)

    def supervisor_probe_fixture(self, *, missing=None, token=True, address=True,
                                 inspect_exit=0, status=200, curl_exit=0, timeout=False,
                                 token_exec_exit=0, cli_env_null=False, operation=False,
                                 jq_program_invalid=False):
        """Execute the actual POSIX probe with real jq and controlled commands."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tools = root / "bin"
            tools.mkdir()
            jq = host.shutil.which("jq")
            self.assertIsNotNone(jq, "jq is required for the real shell regression")
            if missing != "jq":
                if jq_program_invalid:
                    (tools / "jq").write_text("#!/bin/sh\nexec " + jq + " '['\n")
                    (tools / "jq").chmod(0o700)
                else:
                    (tools / "jq").symlink_to(jq)
            cli = [{"Config": {"Env": ["SUPERVISOR_TOKEN=SYNTHETIC_PRIVATE"] if token else []}}]
            if cli_env_null:
                cli[0]["Config"]["Env"] = None
            supervisor = [{"NetworkSettings": {"Networks": {
                "fixture_network": {"IPAddress": "192.0.2.123" if address else ""}}}}]
            docker = tools / "docker"
            docker.write_text("#!/bin/sh\ncase \"$*\" in\n"
                "'exec hassio_cli printenv SUPERVISOR_TOKEN') printf '%s' '"
                + ("SYNTHETIC_PRIVATE" if token else "") + f"'; exit {token_exec_exit};;\n"
                "'inspect hassio_cli') printf '%s' '" + json.dumps(cli) + "';;\n"
                "'inspect hassio_supervisor') printf '%s' '" + json.dumps(supervisor)
                + f"'; exit {inspect_exit};;\n*) exit 99;;\nesac\n")
            docker.chmod(0o700)
            if missing != "curl":
                curl = tools / "curl"
                curl.write_text("#!/bin/sh\nprintf '%s\\n' "
                                "'{\"result\":\"ok\",\"data\":{\"healthy\":true,\"private\":\"SYNTHETIC_PRIVATE\"}}'\n"
                                f"case \"$*\" in *--write-out*) printf '%s\\n' '{status}';; esac\n"
                                f"printf '%s\\n' 'SYNTHETIC_PRIVATE 192.0.2.123' >&2\nexit {curl_exit}\n")
                curl.chmod(0o700)
            h = host.Host(root)
            def ssh(command, **kwargs):
                if not operation:
                    self.assertFalse(kwargs["check"])
                self.assertEqual(kwargs["timeout"], 30)
                result = host.subprocess.run(["/bin/sh", "-c", command], capture_output=True,
                                             env={"PATH": str(tools)}, input=kwargs.get("data"), timeout=5)
                if timeout:
                    raise host.subprocess.TimeoutExpired("private command", 30,
                                                         output=result.stdout, stderr=result.stderr)
                if kwargs.get("check", True):
                    host.require(result.returncode == 0, "fixture guest command failed")
                return result
            h.ssh = ssh
            result = (h.supervisor("POST", "/backups/new/partial", {"name": "fixture"}, timeout=30)
                      if operation else h._supervisor_boot_info("supervisor_info"))
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(h.boot_progress))
            self.assertNotIn("192.0.2.123", json.dumps(h.boot_progress))
            return result, h.boot_progress.get("supervisor_info", {})

    def test_supervisor_readiness_uses_runtime_token_without_cli_inspect_env(self):
        result, progress = self.supervisor_probe_fixture(cli_env_null=True)
        self.assertTrue(result.get("healthy"))
        self.assertEqual(progress["http_status"], 200)

    def test_old_inspect_token_filter_reproduces_jq_exit_five_on_null_env(self):
        # A synthetic malformed inspect shape reproduces the observed exit code;
        # the failed CI guest's raw inspect payload was intentionally not retained.
        result = host.subprocess.run(["jq", "-er", '.[0].Config.Env[] | '
            'select(startswith("SUPERVISOR_TOKEN=")) | sub("^SUPERVISOR_TOKEN=";"")'],
            input=b'[{"Config":{"Env":null}}]', capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 5)

    def test_supervisor_operations_use_runtime_token_without_cli_inspect_env(self):
        result, _ = self.supervisor_probe_fixture(cli_env_null=True, operation=True)
        self.assertTrue(result.get("healthy"))

    def test_supervisor_probe_records_successful_command_families(self):
        result, progress = self.supervisor_probe_fixture()
        self.assertTrue(result["healthy"])
        for name in ("docker_available_exit", "curl_available_exit", "jq_available_exit",
                     "cli_token_exec_exit", "token_lookup_exit", "supervisor_inspect_exit",
                     "network_lookup_exit", "address_lookup_exit", "curl_exit", "ssh_exit"):
            self.assertEqual(progress[name], 0, name)
        self.assertEqual(progress["network_count"], 1)
        self.assertEqual(progress["http_status"], 200)
        self.assertTrue(progress["api_ok"])

    def test_supervisor_probe_fails_immediately_for_missing_guest_tools(self):
        for tool in ("curl", "jq"):
            with self.subTest(tool=tool), self.assertRaisesRegex(host.BootFailure, "executable missing"):
                self.supervisor_probe_fixture(missing=tool)

    def test_supervisor_probe_rejects_impossible_command_and_jq_errors(self):
        with self.assertRaisesRegex(host.BootFailure, "token command unavailable"):
            self.supervisor_probe_fixture(token_exec_exit=127)
        with self.assertRaisesRegex(host.BootFailure, "jq program invalid"):
            self.supervisor_probe_fixture(jq_program_invalid=True)

    def test_supervisor_probe_records_inspect_and_lookup_failures(self):
        for options, field in (({"inspect_exit": 1}, "supervisor_inspect_exit"),
                               ({"token_exec_exit": 1}, "cli_token_exec_exit"),
                               ({"token": False}, "token_lookup_exit"),
                               ({"address": False}, "address_lookup_exit")):
            with self.subTest(field=field):
                result, progress = self.supervisor_probe_fixture(**options)
                self.assertEqual(result, {})
                self.assertNotEqual(progress[field], 0)
                self.assertNotIn("curl_exit", progress)

    def test_supervisor_probe_records_http_failure_and_timeout(self):
        for status, code in ((401, 22), (503, 22), (0, 7)):
            with self.subTest(status=status):
                result, progress = self.supervisor_probe_fixture(status=f"{status:03}", curl_exit=code)
                self.assertEqual(result, {})
                self.assertEqual(progress["http_status"], status)
                self.assertEqual(progress["curl_exit"], code)
        result, progress = self.supervisor_probe_fixture(timeout=True)
        self.assertEqual(result, {})
        self.assertTrue(progress["command_timeout"])
        self.assertEqual(progress["token_lookup_exit"], 0)
        self.assertIsNone(progress["ssh_exit"])

    def test_supervisor_readiness_keeps_both_endpoint_failures(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.qemu = Mock(poll=Mock(return_value=None))
            h._ssh_ready = Mock(return_value=True)
            h.ssh = Mock(side_effect=[
                SimpleNamespace(returncode=22, stdout=b"\n401\n", stderr=b"HAOS_BOOT curl_exit 22\n"),
                SimpleNamespace(returncode=22, stdout=b"\n503\n", stderr=b"HAOS_BOOT curl_exit 22\n")])
            def once(check, *args):
                value = check()
                if not value:
                    raise RuntimeError("not ready")
                return value
            with patch("haos.host.wait_for", side_effect=once), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    h._wait_boot()
            self.assertEqual(h.boot_progress["stage"], "waiting_for_supervisor")
            self.assertEqual(h.boot_progress["supervisor_info"]["http_status"], 401)
            self.assertEqual(h.boot_progress["root_info"]["http_status"], 503)
            self.assertIsNone(h.boot_progress["supervisor_healthy"])
            self.assertEqual(h.boot_progress["supervisor_state"], "missing")

    def test_supervisor_state_and_health_diagnostics_are_allowlisted(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.qemu = Mock(poll=Mock(return_value=None))
            h._ssh_ready = Mock(return_value=True)
            h._supervisor_boot_info = Mock(side_effect=[
                {"healthy": "SYNTHETIC_PRIVATE"}, {"state": "SYNTHETIC_PRIVATE"}])
            def once(check, *args):
                value = check()
                if not value:
                    raise RuntimeError("not ready")
                return value
            with patch("haos.host.wait_for", side_effect=once), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "not ready"):
                    h._wait_boot()
            self.assertIsNone(h.boot_progress["supervisor_healthy"])
            self.assertEqual(h.boot_progress["supervisor_state"], "other")
            self.assertNotIn("SYNTHETIC_PRIVATE", json.dumps(h.boot_progress))

    def test_non_ci_refused_before_kvm(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            with patch("builtins.open") as opened, self.assertRaises(RuntimeError):
                host.Host(d).preflight()
            opened.assert_not_called()

    def test_root_runner_is_refused_before_acl_or_qemu(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), \
                patch("haos.host.platform.machine", return_value="x86_64"), \
                patch("haos.host.os.getuid", return_value=0):
            h = host.Host(d)
            h.refresh_kvm_access = Mock()
            with self.assertRaisesRegex(RuntimeError, "unprivileged runner"):
                h.preflight()
            h.refresh_kvm_access.assert_not_called()

    def test_missing_kvm_is_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("haos.host.platform.machine", return_value="x86_64"), \
                    patch.object(host.Host, "refresh_kvm_access"), \
                    patch("haos.host.os.getuid", return_value=1001), \
                    patch("builtins.open", side_effect=PermissionError), \
                    self.assertRaises(PermissionError):
                host.Host(d).preflight()

    def test_kvm_create_vm_required_before_physical_disk_check(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("haos.host.platform.machine", return_value="x86_64"), \
                    patch.object(host.Host, "refresh_kvm_access") as refresh, \
                    patch("haos.host.os.getuid", return_value=1001), \
                    patch("builtins.open", mock_open()), \
                    patch("haos.host.fcntl.ioctl", side_effect=[12, 999]) as ioctl, \
                    patch("haos.host.os.close") as close, \
                    patch("haos.host.shutil.disk_usage", return_value=SimpleNamespace(free=11 * host.GIB)), \
                    self.assertRaisesRegex(RuntimeError, "12 GiB"):
                host.Host(d).preflight()
            self.assertEqual([call.args[1] for call in ioctl.call_args_list], [0xAE00, 0xAE01])
            close.assert_called_once_with(999)
            refresh.assert_called_once_with("preflight")

    def test_create_vm_ioctl_failure_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("haos.host.platform.machine", return_value="x86_64"), \
                    patch.object(host.Host, "refresh_kvm_access"), \
                    patch("haos.host.os.getuid", return_value=1001), \
                    patch("builtins.open", mock_open()), \
                    patch("haos.host.fcntl.ioctl", side_effect=[12, OSError("KVM unavailable")]), \
                    self.assertRaises(OSError):
                host.Host(d).preflight()

    def test_cleanup_refuses_unowned_registry(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.registry_launch_attempted = True
            h.run = Mock(return_value=SimpleNamespace(returncode=0, stdout=b"another-owner\n"))
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                h.cleanup()
            self.assertEqual(h.run.call_count, 1)
            self.assertFalse(h.registry_progress["cleanup"]["ownership_match"])
            self.assertEqual(h.registry_progress["cleanup"]["result"], "failure")

    def test_cleanup_finds_owned_container_after_launch_timeout_or_error(self):
        for timed_out in (True, False):
            with self.subTest(timed_out=timed_out), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                ok = SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
                failure = host.subprocess.TimeoutExpired(["private-command"], 180) if timed_out else (
                    SimpleNamespace(returncode=125, stdout=b"", stderr=b"private-error"))
                h.run = Mock(side_effect=[ok, failure])
                with self.assertRaises((host.subprocess.TimeoutExpired, RuntimeError)):
                    h.registry()
                self.assertTrue(h.registry_launch_attempted)
                self.assertIsNone(h.registry_created)
                self.assertIsNone(h.registry_progress["container_created"])
                h.run = Mock(side_effect=[SimpleNamespace(returncode=0, stdout=h.owner.encode()), ok])
                h.cleanup()
                commands = [call.args[0] for call in h.run.call_args_list]
                self.assertEqual(commands[0][:3], ["docker", "container", "inspect"])
                self.assertEqual(commands[0][-1], h.owner)
                self.assertEqual(commands[1], ["docker", "rm", "-f", "-v", h.owner])
                self.assertTrue(h.registry_progress["container_created"])
                self.assertEqual(h.registry_progress["cleanup"]["result"], "removed")
                self.assertNotIn("private-", json.dumps(h.registry_progress))

    def test_cleanup_requires_confirmed_registry_absence(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.registry_launch_attempted = True
            h.registry_created = None
            h.run = Mock(side_effect=[SimpleNamespace(returncode=1, stdout=b"", stderr=b"not found"),
                                     SimpleNamespace(returncode=0, stdout=b"", stderr=b"")])
            h.cleanup()
            self.assertEqual(h.run.call_count, 2)
            self.assertEqual(h.run.call_args.args[0], ["docker", "container", "ls", "--all", "--filter",
                             "name=^/" + h.owner + "$", "--format", "{{.Names}}"])
            self.assertEqual(h.registry_progress["cleanup"]["result"], "absent")
            self.assertFalse(h.registry_progress["container_created"])

    def test_cleanup_inspect_failure_is_not_absence(self):
        for listing_exit, listing_output in ((1, b""), (0, b"present-container")):
            with self.subTest(listing_exit=listing_exit), tempfile.TemporaryDirectory() as d:
                h = host.Host(d)
                h.registry_launch_attempted = True
                h.run = Mock(side_effect=[SimpleNamespace(returncode=1, stdout=b"", stderr=b"private-error"),
                    SimpleNamespace(returncode=listing_exit, stdout=listing_output, stderr=b"private-error")])
                with self.assertRaises(RuntimeError):
                    h.cleanup()
                self.assertEqual(h.run.call_count, 2)
                self.assertEqual(h.registry_progress["cleanup"]["result"], "failure")
                self.assertNotIn("private-error", json.dumps(h.registry_progress))

    def test_cleanup_registry_inspect_timeout_is_failure(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.registry_launch_attempted = True
            h.run = Mock(side_effect=host.subprocess.TimeoutExpired(["private-command"], 5))
            with self.assertRaises(host.subprocess.TimeoutExpired):
                h.cleanup()
            self.assertEqual(h.run.call_count, 1)
            self.assertEqual(h.registry_progress["cleanup"]["exception"], "TimeoutExpired")
            self.assertEqual(h.registry_progress["cleanup"]["result"], "failure")

    def test_supervisor_secret_body_is_stdin_only(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.ssh = Mock(return_value=SimpleNamespace(stdout=b'{"result":"ok","data":{}}'))
            h.supervisor("POST", "/addons/local_woow-omnigent/options", {"password": "synthetic-secret"})
            call = h.ssh.call_args
            self.assertNotIn("synthetic-secret", call.args[0])
            self.assertIn(b"synthetic-secret", call.kwargs["data"])

    def test_supervisor_path_injection_refused(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(RuntimeError):
            host.Host(d).supervisor("GET", "/info; echo unexpected")


class AcceptanceTests(unittest.TestCase):
    def test_install_uses_bound_guest_manifest_for_baseline_and_candidate(self):
        for baseline in (True, False):
            with self.subTest(baseline=baseline), tempfile.TemporaryDirectory() as d:
                version = "0.1.23" if baseline else "0.1.24"
                config, config_id, manifest, pushed_id = registry_fixture(version)
                suite = acceptance.Acceptance.__new__(acceptance.Acceptance)
                h = suite.host = host.Host(d)
                h.image_identities[config_id] = provenance.registry_identity(manifest, pushed_id, config, config_id)
                suite.old_image = suite.new_image = {"Id": config_id}
                suite.old_config = {"version": "0.1.23", "options": {}}
                suite.config = {"version": "0.1.24", "options": {}}
                suite.username, suite.password = "fixture-user", "fixture-password"
                suite.pin, suite.evidence = {"fixture": "pin"}, {}
                suite.wait_addon = Mock()
                h.install_config, h.supervisor = Mock(), Mock()
                def ssh(command, **kwargs):
                    if command.startswith("docker inspect app_"):
                        value = json.dumps([{"Image": pushed_id, "Name": "/app_local_woow-omnigent"}])
                    elif " cat /usr/share/omnigent/artifact-pin.json" in command:
                        value = json.dumps(suite.pin)
                    elif "SHOW server_version" in command:
                        value = "16.10"
                    elif "SELECT version_num" in command:
                        value = "fixture_head"
                    else:
                        value = ""
                    return SimpleNamespace(stdout=value.encode())
                h.ssh = Mock(side_effect=ssh)
                h.verify_guest_image = Mock(wraps=h.verify_guest_image)
                suite.install(baseline=baseline)
                h.verify_guest_image.assert_called_once_with(config_id)
                self.assertEqual(suite.container, "app_local_woow-omnigent")

    def test_backup_restore_and_coldboot_use_bound_guest_manifest_for_both_versions(self):
        for version in ("0.1.23", "0.1.24"):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as d:
                config, config_id, manifest, pushed_id = registry_fixture(version)
                suite = acceptance.Acceptance.__new__(acceptance.Acceptance)
                h = suite.host = host.Host(d)
                h.image_identities[config_id] = provenance.registry_identity(manifest, pushed_id, config, config_id)
                suite.old_image = suite.new_image = {"Id": config_id}
                suite.config = {"version": version}
                suite.wait_addon = Mock()
                suite.data_preserved = Mock()
                suite.api = Mock()
                suite.hooks = Mock()
                suite.state = {}
                h.supervisor = Mock(side_effect=[{"slug": "abcd"}, {"addons": [{"slug": acceptance.ADDON}]}, {}])
                starts, boots = iter((b"before", b"after")), iter((b"kernel-before", b"kernel-after"))
                def ssh(command, **kwargs):
                    if "StartedAt" in command:
                        data = next(starts)
                    elif "boot_id" in command:
                        data = next(boots)
                    else:
                        data = json.dumps([{"Image": pushed_id, "Name": "/app_local_woow-omnigent"}]).encode()
                    return SimpleNamespace(stdout=data)
                h.ssh = Mock(side_effect=ssh)
                h.verify_guest_image = Mock(wraps=h.verify_guest_image)
                self.assertEqual(suite.backup("baseline" if version == "0.1.23" else "candidate"), "abcd")
                self.assertEqual(h.verify_guest_image.call_count, 2)
                suite.restore("abcd", version, config_id)
                h.cold_shutdown, h.boot, h.registry_tunnel = Mock(), Mock(), Mock()
                suite.cold_boot()
                self.assertEqual(h.verify_guest_image.call_count, 4)
                self.assertTrue(all(c.args == (config_id,) for c in h.verify_guest_image.call_args_list))

    def test_registry_final_identity_guards_remain_enforced_and_identified(self):
        for guard, new_id, revision in (("candidate_producer_image_id", "wrong", "source"),
                                         ("candidate_core_source", "expected", "wrong"),
                                         ("verified", "expected", "source"),
                                         ("verified", "linked-config", "source")):
            with self.subTest(guard=guard):
                suite = acceptance.Acceptance.__new__(acceptance.Acceptance)
                suite.host = host.Host("/unused-fixture-path")
                suite.host.registry = Mock()
                suite.host.publish_local = Mock(side_effect=[{}, {
                    "Id": new_id, "Config": {"Labels": {"org.opencontainers.image.revision": revision}}}])
                suite.config = {"version": "0.1.24"}
                suite.producer = {"image_id": "expected", "checkout_commit": HEAD, "addon_tree": TREE}
                suite.baseline_identity = {"config_digest": "baseline-config", "producer_image_id": "baseline-id"}
                suite.candidate_identity = {"config_digest": "linked-config", "producer_image_id": "expected",
                                            "producer_id_kind": "manifest"}
                suite.pin = {"sourceCommit": "source", "wheelSha256": "wheel"}
                suite.evidence = {"candidate": {}}
                if guard == "verified":
                    suite.registry()
                else:
                    with self.assertRaises(RuntimeError):
                        suite.registry()
                    self.assertEqual(suite.evidence["candidate"], {})
                self.assertEqual(suite.host.registry_progress["stage"], guard)

    def test_registry_failure_reaches_receipt_without_raw_command_data(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {
                "RUNNER_TEMP": d, "HAOS_REPORT_DIR": d}):
            suite = acceptance.Acceptance(Path(d))
            suite.phase = "private image registry and image identities"
            suite.run = suite.host.registry
            suite.host.cleanup = Mock()
            suite.host.run = Mock(return_value=SimpleNamespace(
                returncode=125, stdout=b"synthetic-secret", stderr=b"denied /mcp/private_token"))
            with patch("haos.acceptance.Acceptance", return_value=suite), \
                    patch("haos.acceptance.signal.signal"), patch("haos.acceptance.os.umask"), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(acceptance.main(), 1)
            text = (Path(d) / "haos-acceptance.json").read_text()
            report = json.loads(text)
            self.assertFalse(report["passed"])
            self.assertEqual(report["evidence"]["registry_progress"]["stage"], "registry_pull")
            for secret in ("synthetic-secret", "private_token"):
                self.assertNotIn(secret, text)

    def test_cold_backup_requires_observed_container_restart(self):
        suite = acceptance.Acceptance.__new__(acceptance.Acceptance)
        suite.host = Mock()
        suite.old_image = {"Id": "old"}
        suite.new_image = {"Id": "new"}
        suite.config = {"version": "0.1.24"}
        suite.wait_addon = Mock()
        suite.host.verify_guest_image.return_value = "app_local_woow-omnigent"
        suite.host.supervisor.side_effect = [
            {"slug": "abcd"}, {"addons": [{"slug": acceptance.ADDON}]}]
        suite.host.ssh.return_value.stdout = b"same-start-time"
        with self.assertRaisesRegex(RuntimeError, "did not restart"):
            suite.backup("candidate")

    def test_failure_report_does_not_emit_exception_body(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Mock()
            fake.phase = "trusted localhost HTTPS MCP proxy"
            fake.checks, fake.evidence = [], {}
            fake.run.side_effect = RuntimeError("password=secret /mcp/private_omcp_SYNTHETIC")
            output = io.StringIO()
            with patch.dict(os.environ, {"RUNNER_TEMP": d, "HAOS_REPORT_DIR": d}), \
                    patch("haos.acceptance.Acceptance", return_value=fake), \
                    patch("haos.acceptance.signal.signal"), patch("haos.acceptance.os.umask"), \
                    contextlib.redirect_stdout(output):
                self.assertEqual(acceptance.main(), 1)
            text = (Path(d) / "haos-acceptance.json").read_text()
            self.assertFalse(json.loads(text)["passed"])
            for secret in ("password=", "secret", "private_omcp_"):
                self.assertNotIn(secret, text + output.getvalue())
            fake.host.cleanup.assert_called_once()

    def test_workflow_permissions_head_checkout_and_only_safe_upload(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/test-haos-addon.yml"
        workflow = yaml.safe_load(path.read_text())
        self.assertEqual(workflow.get("on", workflow.get(True)), {"pull_request": None})
        self.assertEqual(workflow["permissions"], {"contents": "read", "actions": "read"})
        job = workflow["jobs"]["haos"]
        self.assertEqual(job["runs-on"], "ubuntu-22.04")
        self.assertEqual(job["timeout-minutes"], 60)
        steps = job["steps"]
        self.assertEqual(steps[0]["with"]["ref"], "$" + "{{ github.event.pull_request.head.sha }}")
        upload = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(upload), 1)
        self.assertEqual(upload[0]["with"]["path"], "$" + "{{ runner.temp }}/haos-report/haos-acceptance.json")
        # The harness owns the original ACL snapshot and lifecycle; setup must
        # not grant access before it can save the runner's unmodified ACL.
        self.assertNotIn("setfacl", path.read_text())

    def test_workflow_report_directory_uses_runner_only_in_step_context(self):
        path = Path(__file__).resolve().parents[2] / ".github/workflows/test-haos-addon.yml"
        job = yaml.safe_load(path.read_text())["jobs"]["haos"]
        # GitHub evaluates job env before a runner exists. runner is available
        # in step env, as documented in the Actions context-availability table.
        self.assertNotIn("HAOS_REPORT_DIR", job["env"])
        self.assertTrue(all("runner." not in str(value) for value in job["env"].values()))
        boot = next(step for step in job["steps"] if "python -m haos.acceptance" in step.get("run", ""))
        self.assertEqual(boot["env"]["HAOS_REPORT_DIR"], "$" + "{{ runner.temp }}/haos-report")


class RealTLSFixtureTests(unittest.TestCase):
    def test_actual_trusted_tls_prefix_cookie_isolation_and_log_hygiene(self):
        observed = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                observed.append((self.path, self.headers.get("Cookie"), payload))
                body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        origin = "https://localhost:" + str(port)
        token = "omcp_SYNTHETIC_TLS_TEST"
        try:
            with tempfile.TemporaryDirectory() as d:
                suite = acceptance.Acceptance.__new__(acceptance.Acceptance)
                suite.private = Path(d)
                suite.host = host.Host(d)
                try:
                    with patch("haos.acceptance.DIRECT", "http://127.0.0.1:" + str(server.server_port)), \
                            patch("haos.acceptance.PUBLIC_BASE", origin + "/omnigent"):
                        suite.tls()
                        profile = {"token": token, "connection_url": origin + "/omnigent/mcp/private_" + token}
                        status, result = suite.tls_rpc(profile, "initialize")
                        self.assertEqual(status, 200)
                        self.assertEqual(result["result"], {})
                        self.assertEqual(observed[0][:2], ("/mcp/private_" + token, None))
                        self.assertEqual(observed[0][2]["method"], "initialize")
                        with self.assertRaises(httpx.ConnectError):
                            httpx.get(origin, trust_env=False)
                        for log in Path(d).glob("*.log"):
                            self.assertNotIn(token, log.read_text())
                finally:
                    suite.host.cleanup()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
