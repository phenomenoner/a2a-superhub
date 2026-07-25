from __future__ import annotations

import json
import hashlib
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest.mock import patch

from a2a_superhub import operations as operations_module
from a2a_superhub.artifacts import ArtifactStore
from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, atomic_write, note_path, serialize_note
from a2a_superhub.operations import (
    BackupManager,
    BackupSafetyError,
    MigrationError,
    OperationsDiagnostics,
    OperationsError,
    RetentionBlockedError,
    RetentionManager,
    RestoreSafetyError,
    SearchMigrationManager,
    StateLease,
    load_search_provider_config,
)
from a2a_superhub.server import make_server
from a2a_superhub.store import HubStore


OWNER = Principal(
    "agent.alpha", "agent", "tok_owner",
    frozenset({"memory.read", "memory.write", "memory.share", "artifact.read", "artifact.write", "artifact.share"}),
)
RECIPIENT = Principal("agent.beta", "agent", "tok_recipient", frozenset({"memory.read"}))
ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"hub.admin"}))


class _FakeProvider:
    indexes: dict[tuple[str, str | None], list[dict]] = {}

    def __init__(self, state, *, mode, url=None, cache_dir=None):
        self.mode = mode
        self.url = url
        self.key = (mode, url)

    def rebuild(self, notes):
        self.indexes[self.key] = list(notes)
        return {"collection": f"{self.mode}-collection", "notes": len(self.indexes[self.key])}

    def search(self, query, principal, *, load_note, can_read, limit=50):
        notes = [note for note in self.indexes[self.key] if can_read(principal, note)]
        if self.mode == "server" and self.url and "divergent" in self.url:
            notes = list(reversed(notes))
        return notes[:limit]

    def capabilities(self):
        return {"mode": self.mode, "version": "test", "available": True}


class OperationalReadinessScenarios(unittest.TestCase):
    def test_http_diagnostics_coalesces_refresh_and_serves_last_completed_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeded = self._seed(root)
            principals = {
                "admin-token": {
                    "subject": "local.operator", "kind": "operator",
                    "tokenId": "tok_admin_http", "scopes": ["hub.admin"],
                },
            }
            server = make_server(seeded["state"], host="127.0.0.1", port=0, principals=principals)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/v1/operations/diagnostics"

            def read_diagnostics():
                request = urllib.request.Request(
                    url, headers={"Authorization": "Bearer admin-token"},
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)

            refresh_started = threading.Event()
            release_refresh = threading.Event()
            first_finished = threading.Event()
            retry_finished = threading.Event()
            first_results: list[dict] = []
            retry_results: list[dict] = []
            failures: list[BaseException] = []
            inventory_calls = 0
            original_inventory = operations_module._state_inventory

            def blocked_inventory(state):
                nonlocal inventory_calls
                inventory_calls += 1
                refresh_started.set()
                if not release_refresh.wait(5):
                    raise AssertionError("test did not release diagnostics refresh")
                return original_inventory(state)

            def read_into(results, finished):
                try:
                    results.append(read_diagnostics())
                except BaseException as exc:  # pragma: no cover - asserted below
                    failures.append(exc)
                finally:
                    finished.set()

            try:
                completed_snapshot = read_diagnostics()
                with patch.object(
                    operations_module, "_state_inventory", side_effect=blocked_inventory,
                ):
                    first = threading.Thread(
                        target=read_into, args=(first_results, first_finished),
                        name="diagnostics-refresh",
                    )
                    first.start()
                    self.assertTrue(refresh_started.wait(5), "diagnostics refresh did not start")
                    retry = threading.Thread(
                        target=read_into, args=(retry_results, retry_finished),
                        name="diagnostics-retry",
                    )
                    retry.start()
                    retry_returned_while_refresh_blocked = retry_finished.wait(1)
                    release_refresh.set()
                    first.join(timeout=10)
                    retry.join(timeout=10)
            finally:
                release_refresh.set()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

            self.assertTrue(
                retry_returned_while_refresh_blocked,
                "retry started a second corpus inventory instead of using the completed snapshot",
            )
            self.assertFalse(first.is_alive())
            self.assertFalse(retry.is_alive())
            self.assertEqual([], failures)
            self.assertEqual(1, inventory_calls)
            self.assertEqual(completed_snapshot, retry_results[0])
            self.assertTrue(first_results[0]["payloadFree"])

    def _seed(self, root: Path) -> dict:
        state = root / "state"
        store = HubStore(state)
        store.init()
        task, _ = store.create_task({
            "fromAgent": "agent.alpha", "toAgent": "agent.beta", "intent": "agent.handoff",
            "idempotencyKey": "ops-seed-task", "payload": {"summary": "handoff"},
        })
        store.append_event(task["taskId"], "task.completed", {"result": "ok"}, state="completed")

        artifacts = ArtifactStore(state)
        artifact = artifacts.put_bytes(
            b"authoritative artifact bytes", filename="evidence.bin",
            created_by=OWNER.subject, visibility="shared",
        )

        memory = MemoryService(state, enable_delivery=True)
        note = memory.create_note({
            "type": "handoff", "title": "restore marker", "visibility": "shared",
            "about": [RECIPIENT.subject], "body": "private restore sentinel",
        }, OWNER, idempotency_key="ops-seed-note").note
        inbox = memory.fetch_inbox(RECIPIENT, "desktop")
        self.assertEqual([note["id"]], [item["note"]["id"] for item in inbox["items"]])
        memory.acknowledge_inbox(RECIPIENT, "desktop", inbox["cursor"])

        auth = root / "principals.json"
        auth.write_text(json.dumps({
            "operator-secret-token": {
                "subject": "local.operator", "kind": "operator", "tokenId": "tok_operator",
                "scopes": ["hub.admin"],
            }
        }), encoding="utf-8")
        return {"state": state, "task": task, "artifact": artifact, "note": note, "auth": auth}

    def test_authoritative_backup_restores_clean_state_and_rebuilds_only_derived_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeded = self._seed(root)
            archive = root / "private-backup.zip"
            result = BackupManager(seeded["state"]).create(
                archive, auth_config=seeded["auth"], target_class="private",
            )
            self.assertEqual("a2a-superhub.backup.v1", result["schema"])
            self.assertNotIn("operator-secret-token", json.dumps(result))

            restored = root / "restored"
            restore = BackupManager.restore(archive, restored)
            self.assertEqual("verified", restore["integrity"])
            self.assertEqual(seeded["task"]["taskId"], HubStore(restored).get_task(seeded["task"]["taskId"])["taskId"])
            self.assertEqual(b"authoritative artifact bytes", ArtifactStore(restored).get_bytes(seeded["artifact"]["artifactId"]))
            restored_memory = MemoryService(restored, enable_delivery=True)
            self.assertEqual("private restore sentinel", restored_memory.read_note(seeded["note"]["id"], OWNER)["body"])
            self.assertEqual([], restored_memory.fetch_inbox(RECIPIENT, "desktop")["items"])
            self.assertTrue((restored / "memory" / "index.sqlite").is_file())
            self.assertFalse((restored / "retrieval" / "qdrant").exists())
            self.assertTrue((restored / "config" / "principals.json").is_file())

    def test_public_backup_guard_fails_closed_and_override_is_auditable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeded = self._seed(root)
            refused = root / "public-refused.zip"
            with self.assertRaisesRegex(BackupSafetyError, "sensitive"):
                BackupManager(seeded["state"]).create(
                    refused, auth_config=seeded["auth"], target_class="public",
                )
            self.assertFalse(refused.exists())

            allowed = root / "public-explicit.zip"
            result = BackupManager(seeded["state"]).create(
                allowed, auth_config=seeded["auth"], target_class="public",
                allow_sensitive_public=True,
            )
            self.assertTrue(result["containsSensitive"])
            self.assertTrue(result["warnings"])
            with zipfile.ZipFile(allowed) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual("public", manifest["targetClass"])
            self.assertTrue(manifest["overrideRecorded"])
            self.assertNotIn(str(root), json.dumps(manifest))

    def test_restore_rejects_tampered_members_without_exposing_a_partial_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeded = self._seed(root)
            archive_path = root / "backup.zip"
            BackupManager(seeded["state"]).create(archive_path)
            tampered = root / "tampered.zip"
            with zipfile.ZipFile(archive_path) as source, zipfile.ZipFile(tampered, "w") as destination:
                for info in source.infolist():
                    value = source.read(info.filename)
                    if info.filename == "tasks/hub-tasks.sqlite":
                        value += b"tamper"
                    destination.writestr(info, value)
            target = root / "must-not-exist"
            with self.assertRaisesRegex(RestoreSafetyError, "size|integrity"):
                BackupManager.restore(tampered, target)
            self.assertFalse(target.exists())

    def test_retention_preserves_unread_and_private_state_then_restores_note_and_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            memory = MemoryService(state, enable_delivery=True)
            shared = memory.create_note({
                "type": "handoff", "title": "unread", "visibility": "shared",
                "about": [RECIPIENT.subject], "body": "retain until acknowledged",
            }, OWNER, idempotency_key="retention-shared").note
            retention = RetentionManager(state)
            with self.assertRaisesRegex(RetentionBlockedError, "unacknowledged"):
                retention.trash_note(shared["id"], ADMIN)
            inbox = memory.fetch_inbox(RECIPIENT, "desktop")
            memory.acknowledge_inbox(RECIPIENT, "desktop", inbox["cursor"])
            trashed = retention.trash_note(shared["id"], ADMIN)
            self.assertEqual("trashed", trashed["state"])
            with self.assertRaises(KeyError):
                MemoryService(state).read_note(shared["id"], ADMIN)
            retention.restore("memory-note", shared["id"], ADMIN)
            self.assertEqual("retain until acknowledged", MemoryService(state).read_note(shared["id"], ADMIN)["body"])

            private = memory.create_note({
                "type": "observation", "title": "private", "visibility": "private", "body": "protected",
            }, OWNER, idempotency_key="retention-private").note
            with self.assertRaisesRegex(RetentionBlockedError, "private"):
                retention.trash_note(private["id"], ADMIN)

            artifact = ArtifactStore(state).put_bytes(
                b"recoverable", created_by=OWNER.subject, visibility="private",
            )
            with self.assertRaisesRegex(RetentionBlockedError, "private"):
                retention.trash_artifact(artifact["artifactId"], ADMIN)
            retention.trash_artifact(artifact["artifactId"], ADMIN, allow_private=True)
            self.assertIsNone(ArtifactStore(state).get_manifest(artifact["artifactId"]))
            retention.restore("artifact", artifact["artifactId"], ADMIN)
            self.assertEqual(b"recoverable", ArtifactStore(state).get_bytes(artifact["artifactId"]))

    def test_retention_recovers_process_stops_on_both_sides_of_the_atomic_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            memory = MemoryService(state)
            note = memory.create_note({
                "type": "observation", "title": "transition", "visibility": "shared",
                "body": "recover transition",
            }, OWNER, idempotency_key="retention-transition").note
            memory.init()
            original = memory._authoritative_catalog[note["id"]]
            trash = state / "trash" / "memory" / note["id"] / "note.md"
            trash.parent.mkdir(parents=True)
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            retention = RetentionManager(state)
            retention._init()
            retention._prepare_tombstone(
                kind="memory-note", object_id=note["id"], original=original,
                trash=trash, digest=digest, metadata={"visibility": "shared"},
            )
            original.replace(trash)

            recovered_trash = retention.trash_note(note["id"], ADMIN)
            self.assertTrue(recovered_trash["idempotent"])
            retention._set_tombstone_state("memory-note", note["id"], "restoring")
            original.parent.mkdir(parents=True, exist_ok=True)
            trash.replace(original)

            recovered_restore = retention.restore("memory-note", note["id"], ADMIN)
            self.assertTrue(recovered_restore["idempotent"])
            self.assertEqual("recover transition", MemoryService(state).read_note(note["id"], ADMIN)["body"])

    def test_retention_normalizes_an_equivalent_state_path_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            memory = MemoryService(state)
            note = memory.create_note({
                "type": "observation", "title": "alias", "visibility": "shared",
                "body": "canonical relative tombstone",
            }, OWNER, idempotency_key="retention-alias").note
            memory.init()
            alias = root / "state-alias"
            try:
                os.symlink(state, alias, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory link unavailable: {exc}")
            original = memory._authoritative_catalog[note["id"]]
            trash = alias / "trash" / "memory" / note["id"] / "note.md"
            trash.parent.mkdir(parents=True)
            retention = RetentionManager(state)
            retention._init()
            retention._prepare_tombstone(
                kind="memory-note",
                object_id=note["id"],
                original=original,
                trash=trash,
                digest=hashlib.sha256(original.read_bytes()).hexdigest(),
                metadata={"visibility": "shared"},
            )
            row = retention._tombstone("memory-note", note["id"])
            self.assertEqual(f"trash/memory/{note['id']}/note.md", row["trash_path"])

    def test_retention_relative_paths_still_reject_state_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            state.mkdir()
            outside = root / "outside.md"
            outside.write_text("outside", encoding="utf-8")
            with self.assertRaisesRegex(OperationsError, "escapes the state root"):
                RetentionManager(state)._relative_state_path(outside)

    def test_backup_excludes_active_runtime_and_diagnostics_are_payload_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeded = self._seed(root)
            with StateLease(seeded["state"], purpose="runtime"):
                with self.assertRaisesRegex(BackupSafetyError, "active"):
                    BackupManager(seeded["state"]).create(root / "locked.zip")
            server = make_server(seeded["state"], host="127.0.0.1", port=0)
            try:
                with self.assertRaisesRegex(BackupSafetyError, "active"):
                    BackupManager(seeded["state"]).create(root / "server-locked.zip")
                with self.assertRaisesRegex(OperationsError, "active"):
                    RetentionManager(seeded["state"]).trash_note(seeded["note"]["id"], ADMIN)
            finally:
                server.server_close()
            self.assertEqual(
                "a2a-superhub.backup.v1",
                BackupManager(seeded["state"]).create(root / "after-server-close.zip")["schema"],
            )
            diagnostics = OperationsDiagnostics(seeded["state"]).collect(ADMIN)
            rendered = json.dumps(diagnostics)
            self.assertEqual("a2a-superhub.operations-diagnostics.v1", diagnostics["schema"])
            self.assertGreaterEqual(diagnostics["stores"]["tasks"]["records"], 1)
            self.assertGreaterEqual(diagnostics["stores"]["artifacts"]["records"], 1)
            self.assertEqual(0, diagnostics["stores"]["memory"]["index"]["lagRecords"])
            self.assertNotIn("private restore sentinel", rendered)
            self.assertNotIn("operator-secret-token", rendered)
            self.assertNotIn(str(root), rendered)
            changed = dict(seeded["note"])
            changed["body"] = "authoritative state ahead of derived index"
            atomic_write(note_path(seeded["state"] / "memory", changed["id"]), serialize_note(changed))
            stale = OperationsDiagnostics(seeded["state"]).collect(ADMIN)
            self.assertEqual(1, stale["stores"]["memory"]["index"]["lagRecords"])

    def test_search_migration_requires_parity_and_has_explicit_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "state"
            memory = MemoryService(state)
            for index in range(2):
                memory.create_note({
                    "type": "observation", "title": f"note {index}", "visibility": "shared",
                    "body": f"migration marker {index}",
                }, OWNER, idempotency_key=f"migration-{index}")
            manager = SearchMigrationManager(state, provider_factory=_FakeProvider)
            with self.assertRaisesRegex(MigrationError, "parity"):
                manager.drill(
                    "http://divergent.invalid", queries=[{"text": "migration", "principal": OWNER.subject}],
                    activate=True,
                )
            self.assertFalse(manager.config_path.exists())

            result = manager.drill(
                "http://qdrant.invalid", queries=[{"text": "migration", "principal": OWNER.subject}],
                activate=True,
            )
            self.assertEqual(1.0, result["queryParity"])
            self.assertEqual("server", json.loads(manager.config_path.read_text())["mode"])
            self.assertEqual(
                {"mode": "server", "url": "http://qdrant.invalid"},
                load_search_provider_config(state),
            )
            rollback = manager.rollback()
            self.assertEqual("local", rollback["mode"])
            self.assertEqual("local", json.loads(manager.config_path.read_text())["mode"])
            self.assertEqual({"mode": "local", "url": None}, load_search_provider_config(state))

    def test_http_diagnostics_is_admin_only_and_payload_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seeded = self._seed(root)
            principals = {
                "admin-token": {
                    "subject": "local.operator", "kind": "operator", "tokenId": "tok_admin_http",
                    "scopes": ["hub.admin"],
                },
                "reader-token": {
                    "subject": "agent.beta", "kind": "agent", "tokenId": "tok_reader_http",
                    "scopes": ["memory.read"],
                },
            }
            server = make_server(seeded["state"], host="127.0.0.1", port=0, principals=principals)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://127.0.0.1:{server.server_port}/v1/operations/diagnostics"
            try:
                request = urllib.request.Request(url, headers={"Authorization": "Bearer admin-token"})
                with urllib.request.urlopen(request) as response:
                    payload = json.load(response)
                self.assertTrue(payload["payloadFree"])
                self.assertNotIn("private restore sentinel", json.dumps(payload))
                denied = urllib.request.Request(url, headers={"Authorization": "Bearer reader-token"})
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(denied)
                self.assertEqual(403, failure.exception.code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
