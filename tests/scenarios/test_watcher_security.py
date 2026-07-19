import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from a2a_superhub.client import HubClient, HubClientError
from a2a_superhub.memory import serialize_note
from a2a_superhub.server import _event_touches_markdown, _safe_markdown_snapshot, make_server


class RuntimeWatcherSecurityTests(unittest.TestCase):
    def test_atomic_temp_to_markdown_move_is_a_runtime_event(self):
        event = type(
            "Moved",
            (),
            {"src_path": "memory/notes/.write.tmp", "dest_path": "memory/notes/aa/mem_demo.md"},
        )()
        self.assertTrue(_event_touches_markdown(event))

    def test_polling_snapshot_survives_atomic_disappearance(self):
        root = Path("memory/notes")
        vanished = Mock()
        vanished.__str__ = Mock(return_value="memory/notes/vanished.md")
        vanished.stat.side_effect = FileNotFoundError("atomic source moved")
        vanished.relative_to.return_value = Path("vanished.md")
        snapshot = _safe_markdown_snapshot(root, [vanished])
        self.assertEqual((("vanished.md", -1, -1),), snapshot)

    def test_duplicate_external_id_converges_before_any_surface_serves_content(self):
        principals = {
            "alpha-token": {
                "subject": "agent.alpha", "kind": "agent", "tokenId": "tok_alpha",
                "scopes": ["memory.read", "memory.write", "memory.share"],
            },
            "beta-token": {
                "subject": "agent.beta", "kind": "agent", "tokenId": "tok_beta",
                "scopes": ["memory.read"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(tmp, port=0, enable_memory=True, enable_delivery=True, principals=principals)
            self.assertTrue(getattr(server, "runtime_watcher_enabled", False), "memory-core watchdog runtime required")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            alpha = HubClient(base, token="alpha-token")
            beta = HubClient(base, token="beta-token")
            try:
                created = alpha.create_note(
                    {
                        "type": "observation", "title": "Duplicate boundary", "visibility": "direct:agent.beta",
                        "about": ["agent.beta"], "body": "must fail closed after duplicate event",
                    },
                    "watcher-duplicate",
                )
                note = beta.read_note(created["id"])
                convergence = server.memory_convergence_event
                self.assertTrue(convergence.wait(5), "initial API note did not converge")
                convergence.clear()
                duplicate = Path(tmp) / "memory" / "notes" / "human" / "duplicate.md"
                duplicate.parent.mkdir(parents=True, exist_ok=True)
                duplicate.write_bytes(serialize_note(note))
                self.assertTrue(convergence.wait(8), "runtime watcher did not converge the duplicate ID")
                self.assertEqual([], beta.search("must fail closed")["items"])

                with self.assertRaises(HubClientError):
                    beta.read_note(created["id"])
                graph = beta.request("GET", "/v1/memory/graph", query={"node": f"note:{created['id']}"})
                self.assertEqual([], graph["nodes"])
                wakeup = beta.wakeup("duplicate-security")
                self.assertNotIn("must fail closed", str(wakeup))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
