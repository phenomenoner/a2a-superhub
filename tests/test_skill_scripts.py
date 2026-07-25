import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from a2a_superhub.server import make_server


ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "skills" / "operate-a2a-superhub" / "scripts" / "doctor.py"
BOOTSTRAP = ROOT / "skills" / "operate-a2a-superhub" / "scripts" / "bootstrap_local.py"
LAUNCH_MCP = ROOT / "skills" / "operate-a2a-superhub" / "scripts" / "launch_mcp.py"
SMOKE = ROOT / "skills" / "operate-a2a-superhub" / "scripts" / "smoke.py"


class SkillScriptTests(unittest.TestCase):
    def run_script(self, script, *args, env=None):
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(ROOT / "src")
        if env:
            child_env.update(env)
        return subprocess.run(
            [sys.executable, str(script), *args],
            cwd=ROOT,
            env=child_env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
        )

    def test_doctor_is_read_only_and_never_prints_token(self):
        secret = "doctor-secret-never-print"
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(tmp, port=0, token=secret, enable_memory=True, enable_delivery=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = self.run_script(
                    DOCTOR, "--url", f"http://127.0.0.1:{server.server_port}", "--json",
                    env={"A2A_SUPERHUB_TOKEN": secret},
                )
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                self.assertNotIn(secret, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["ok"])
                self.assertTrue(payload["readOnly"])
                self.assertEqual(payload["compatibility"], "current")
                self.assertEqual(payload["transport"]["selected"], "mcp")
                self.assertEqual(payload["transport"]["resourceRefresh"], "subscribe")
                self.assertEqual(10, len(payload["transport"]["mcp"]["tools"]))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_doctor_wrong_token_is_distinct_auth_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(tmp, port=0, token="correct-secret")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = self.run_script(
                    DOCTOR, "--url", f"http://127.0.0.1:{server.server_port}", "--json",
                    env={"A2A_SUPERHUB_TOKEN": "wrong-secret"},
                )
                self.assertEqual(result.returncode, 3)
                self.assertEqual(json.loads(result.stdout)["error"]["kind"], "auth")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_smoke_default_is_ephemeral_and_existing_target_requires_authority(self):
        result = self.run_script(SMOKE, "--json")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ephemeral"])
        self.assertEqual(payload["wakeupRole"], "data")
        self.assertEqual(payload["unreadAfterAck"], 0)

        refused = self.run_script(SMOKE, "--url", "http://127.0.0.1:1", "--json")
        self.assertEqual(refused.returncode, 2)
        self.assertEqual(json.loads(refused.stdout)["error"]["kind"], "authority")

    def test_local_bootstrap_creates_private_profiles_without_printing_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            result = self.run_script(BOOTSTRAP, "--root", str(runtime), "--json")
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            registry = json.loads((runtime / "principals.json").read_text(encoding="utf-8"))
            profiles = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((runtime / "connections").glob("*.json"))
            ]
            self.assertEqual(
                {"agent.alpha", "agent.beta", "local.operator"},
                {profile["subject"] for profile in profiles},
            )
            self.assertEqual(set(registry), {profile["token"] for profile in profiles})
            for token in registry:
                self.assertNotIn(token, result.stdout + result.stderr)
            refused = self.run_script(BOOTSTRAP, "--root", str(runtime), "--json")
            self.assertEqual(refused.returncode, 2)
            self.assertIn("refusing to overwrite", json.loads(refused.stderr)["error"]["message"])

    def test_connection_profile_check_binds_expected_authenticated_subject(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "runtime"
            bootstrapped = self.run_script(BOOTSTRAP, "--root", str(runtime), "--json")
            self.assertEqual(bootstrapped.returncode, 0, bootstrapped.stderr + bootstrapped.stdout)
            registry = json.loads((runtime / "principals.json").read_text(encoding="utf-8"))
            profile_path = runtime / "connections" / "agent.alpha.json"
            secret = json.loads(profile_path.read_text(encoding="utf-8"))["token"]
            server = make_server(
                str(Path(tmp) / "state"),
                port=0,
                principals=registry,
                enable_memory=True,
                enable_delivery=True,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                profile["url"] = f"http://127.0.0.1:{server.server_port}"
                profile_path.write_text(json.dumps(profile), encoding="utf-8")
                result = self.run_script(
                    LAUNCH_MCP,
                    "--check",
                    "--json",
                    env={"A2A_SUPERHUB_CONNECTION_FILE": str(profile_path)},
                )
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                payload = json.loads(result.stdout)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["subject"], "agent.alpha")
                self.assertNotIn(secret, result.stdout + result.stderr)

                profile["subject"] = "agent.beta"
                profile_path.write_text(json.dumps(profile), encoding="utf-8")
                refused = self.run_script(
                    LAUNCH_MCP,
                    "--check",
                    "--json",
                    env={"A2A_SUPERHUB_CONNECTION_FILE": str(profile_path)},
                )
                self.assertEqual(refused.returncode, 2)
                self.assertNotIn(secret, refused.stdout + refused.stderr)
                self.assertIn("does not match", json.loads(refused.stderr)["error"]["message"])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_connection_profile_requires_an_absolute_regular_file(self):
        result = self.run_script(
            LAUNCH_MCP,
            "--check",
            "--json",
            env={"A2A_SUPERHUB_CONNECTION_FILE": "relative-profile.json"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"]["kind"], "configuration")


if __name__ == "__main__":
    unittest.main()
