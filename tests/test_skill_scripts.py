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


if __name__ == "__main__":
    unittest.main()
