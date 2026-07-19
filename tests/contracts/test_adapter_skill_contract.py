import json
import tempfile
import threading
import unittest
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    Draft202012Validator = None

from a2a_superhub.adapter import ReferenceAdapter
from a2a_superhub.client import HubClient
from a2a_superhub.server import make_server
from a2a_superhub.skill_package import install_skill


ROOT = Path(__file__).resolve().parents[2]


class AdapterSkillContractTests(unittest.TestCase):
    def setUp(self):
        if Draft202012Validator is None:
            self.skipTest("jsonschema is supplied by the contracts extra")

    def test_live_capabilities_and_adapter_context_match_closed_contract(self):
        schema = json.loads((ROOT / "schemas" / "reference-adapter-v1.schema.json").read_text(encoding="utf-8"))
        principals = {
            "beta-token": {
                "subject": "agent.beta", "kind": "agent", "tokenId": "tok_beta",
                "scopes": ["memory.read"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(tmp, port=0, enable_memory=True, enable_delivery=True, principals=principals)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = HubClient(f"http://127.0.0.1:{server.server_port}", token="beta-token")
                capabilities = client.negotiate()
                capabilities.pop("compatibility")
                capability_root = {"$ref": "#/$defs/capabilities", "$defs": schema["$defs"]}
                Draft202012Validator(capability_root).validate(capabilities)
                adapter = ReferenceAdapter(client, principal="agent.beta", consumer_id="contract-runtime")
                delivered = []
                adapter.start_session(delivered.append)
                context_root = {"$ref": "#/$defs/contextBlock", "$defs": schema["$defs"]}
                Draft202012Validator(context_root).validate(delivered[0])
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_installer_manifest_matches_machine_schema(self):
        schema = json.loads((ROOT / "schemas" / "skill-install-v1.schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as tmp:
            result = install_skill(target="codex", target_root=Path(tmp).resolve())
            manifest = json.loads((Path(result["path"]) / ".a2a-superhub-install.json").read_text(encoding="utf-8"))
            Draft202012Validator(schema).validate(manifest)

    def test_server_core_does_not_depend_on_reference_adapter(self):
        server_source = (ROOT / "src" / "a2a_superhub" / "server.py").read_text(encoding="utf-8")
        self.assertNotIn("from .adapter", server_source)
        self.assertNotIn("import a2a_superhub.adapter", server_source)


if __name__ == "__main__":
    unittest.main()
