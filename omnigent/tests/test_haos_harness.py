"""Harness regression tests. These are not HAOS/Supervisor acceptance evidence."""
import contextlib
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
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


class HostTests(unittest.TestCase):
    def boot_fixture(self, supervisor_info, root_info):
        """Use the real response envelopes, parser and readiness predicate."""
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.disk_guard = Mock()
            h.start = Mock(return_value=Mock(poll=Mock(return_value=None)))
            visited = []
            def ssh(command, **kwargs):
                if command == "true":
                    return SimpleNamespace(returncode=0)
                for path, data in (("/supervisor/info", supervisor_info), ("/info", root_info)):
                    if command.endswith('"http://$address' + path + '"'):
                        visited.append(path)
                        return SimpleNamespace(stdout=json.dumps({"result": "ok", "data": data}).encode())
                self.fail("unexpected guest command")
            h.ssh = Mock(side_effect=ssh)
            def check_once(check, seconds, description, interval=3):
                result = check()
                if not result:
                    raise RuntimeError("not ready")
                return result
            with patch("haos.host.wait_for", side_effect=check_once):
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

    def test_non_ci_refused_before_kvm(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "false"}):
            with patch("builtins.open") as opened, self.assertRaises(RuntimeError):
                host.Host(d).preflight()
            opened.assert_not_called()

    def test_missing_kvm_is_failure(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("haos.host.platform.machine", return_value="x86_64"), \
                    patch("builtins.open", side_effect=PermissionError), \
                    self.assertRaises(PermissionError):
                host.Host(d).preflight()

    def test_kvm_create_vm_required_before_physical_disk_check(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("haos.host.platform.machine", return_value="x86_64"), \
                    patch("builtins.open", mock_open()), \
                    patch("haos.host.fcntl.ioctl", side_effect=[12, 999]) as ioctl, \
                    patch("haos.host.os.close") as close, \
                    patch("haos.host.shutil.disk_usage", return_value=SimpleNamespace(free=11 * host.GIB)), \
                    self.assertRaisesRegex(RuntimeError, "12 GiB"):
                host.Host(d).preflight()
            self.assertEqual([call.args[1] for call in ioctl.call_args_list], [0xAE00, 0xAE01])
            close.assert_called_once_with(999)

    def test_create_vm_ioctl_failure_is_not_skipped(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with patch("haos.host.platform.machine", return_value="x86_64"), \
                    patch("builtins.open", mock_open()), \
                    patch("haos.host.fcntl.ioctl", side_effect=[12, OSError("KVM unavailable")]), \
                    self.assertRaises(OSError):
                host.Host(d).preflight()

    def test_cleanup_refuses_unowned_registry(self):
        with tempfile.TemporaryDirectory() as d:
            h = host.Host(d)
            h.registry_created = True
            h.run = Mock(return_value=SimpleNamespace(stdout=b"another-owner\n"))
            with self.assertRaisesRegex(RuntimeError, "ownership"):
                h.cleanup()
            self.assertEqual(h.run.call_count, 1)

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
        self.assertIn('sudo setfacl -m "u:$(id -un):rw" /dev/kvm', path.read_text())

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
