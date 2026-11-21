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
