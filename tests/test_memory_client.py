import json
import tempfile
import threading
import unittest
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from a2a_superhub.client import HubClient, HubClientError
from a2a_superhub.server import make_server


class LegacyHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_GET(self):
        if self.path == "/v1/capabilities":
            self.send_response(HTTPStatus.NOT_FOUND)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/.well-known/agent-card.json":
            body = json.dumps({"capabilities": {"memoryFoundation": True}}).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Length", "0")
        self.end_headers()


class HubClientCompatibilityTests(unittest.TestCase):
    def test_explicit_missing_current_route_downgrades_to_n_minus_one_read_only(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), LegacyHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = HubClient(f"http://127.0.0.1:{server.server_port}").negotiate()
            self.assertEqual(result["compatibility"], "n-1-read-only")
            self.assertTrue(result["memoryFoundation"])
            self.assertNotIn("safeWakeup", result)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_wrong_token_remains_auth_error_and_never_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(tmp, port=0, token="correct-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(HubClientError) as caught:
                    HubClient(f"http://127.0.0.1:{server.server_port}", token="wrong-token").negotiate()
                self.assertEqual(caught.exception.kind, "auth")
                self.assertEqual(caught.exception.status, 401)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
