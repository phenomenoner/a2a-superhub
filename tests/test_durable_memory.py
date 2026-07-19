from __future__ import annotations

import hashlib
import concurrent.futures
import gc
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
import yaml

from a2a_superhub.auth import Principal
from a2a_superhub.memory import (
    AuthorizationError,
    ConflictError,
    MemoryError,
    MemoryService,
    MemoryWatcher,
    QuarantineError,
    atomic_write,
    note_path,
    parse_note,
    serialize_note,
    _idempotency_lock,
    _idempotency_lock_count,
)


def principal(subject: str, *scopes: str) -> Principal:
    return Principal(subject, "agent", f"tok_{subject.replace('.', '_')}", frozenset(scopes))


OWNER = principal("agent.alpha", "memory.read", "memory.write", "memory.share")
RECIPIENT = principal("agent.beta", "memory.read")
OTHER = principal("agent.gamma", "memory.read")
WRITER = principal("agent.writer", "memory.write")
ADMIN = principal("local.operator", "memory.read", "memory.write", "memory.share", "memory.admin")


class DurableMemoryTests(unittest.TestCase):
    def test_same_compound_idempotency_key_has_one_concurrent_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(
                Path(tmp), new_note_id=lambda: "mem_fefefefefefefefefefefefefefefefe"
            )
            barrier = threading.Barrier(8)

            def create(_: int):
                barrier.wait()
                return service.create_note(
                    {"type": "note", "title": "concurrent", "visibility": "private", "body": "same logical write"},
                    OWNER, idempotency_key="concurrent-same",
                )

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(create, range(8)))

            self.assertEqual(1, sum(result.inserted for result in results))
            self.assertEqual({"mem_fefefefefefefefefefefefefefefefe"}, {result.note["id"] for result in results})
            self.assertEqual(1, len(service.search("same logical write", OWNER)))
            for index in range(100):
                lock = _idempotency_lock(Path(tmp), OWNER.subject, "memory.note.create.api", f"cleanup-{index}")
                with lock:
                    pass
            del lock
            gc.collect()
            self.assertEqual(0, _idempotency_lock_count())

    def test_same_compound_key_different_hash_race_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp))
            barrier = threading.Barrier(2)

            def create(body: str):
                barrier.wait()
                try:
                    return service.create_note(
                        {"type": "note", "title": "race", "visibility": "private", "body": body},
                        OWNER, idempotency_key="concurrent-conflict",
                    )
                except ConflictError as exc:
                    return exc

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, ("first", "second")))

            self.assertEqual(1, sum(isinstance(item, ConflictError) for item in results))
            self.assertEqual(1, sum(not isinstance(item, ConflictError) and item.inserted for item in results))

    def _service(self, root: Path) -> MemoryService:
        identifiers = iter(f"mem_{index:032x}" for index in range(1, 100))
        return MemoryService(root, now=lambda: "2026-07-19T12:00:00Z", new_note_id=lambda: next(identifiers))

    @staticmethod
    def _request(**overrides):
        value = {"type": "note", "title": "Gateway observation", "visibility": "private", "body": "line one\r\nline two"}
        value.update(overrides)
        return value

    def test_roundtrip_preserves_utf8_lf_and_safe_frontmatter(self) -> None:
        note = {
            "schema": "a2a-superhub.memory.note.v1",
            "id": "mem_11111111111111111111111111111111",
            "type": "note",
            "title": "Unicode 測試",
            "author": "agent.alpha",
            "visibility": "private",
            "recordedAt": "2026-07-19T12:00:00Z",
            "source": {"kind": "filesystem"},
            "relations": [{"type": "about", "target": "agent.beta"}],
            "body": "甲\r\n乙",
        }
        encoded = serialize_note(note)
        parsed = parse_note(b"\xef\xbb\xbf" + encoded.replace(b"\n", b"\r\n"))
        self.assertEqual("甲\n乙", parsed["body"])
        with self.assertRaises(QuarantineError):
            parse_note(b"---\nx: !!python/object/apply:os.system ['whoami']\n---\nbody")

    def test_runtime_validator_matches_closed_v1_schema_constraints(self) -> None:
        base = {
            "schema": "a2a-superhub.memory.note.v1",
            "id": "mem_11111111111111111111111111111111",
            "type": "note",
            "title": "valid",
            "author": "agent.alpha",
            "visibility": "private",
            "recordedAt": "2026-07-19T12:00:00Z",
            "source": {"kind": "filesystem"},
            "body": "body",
        }
        invalid = [
            {**base, "unexpected": True},
            {**base, "author": 1},
            {**base, "recordedAt": "not-a-date"},
            {**base, "recordedAt": "2026-07-19 12:00:00+00:00"},
            {**base, "source": {"kind": "api", "unexpected": True}},
            {**base, "source": {"kind": "api", "artifactId": 1}},
            {**base, "participants": ["agent.alpha", "agent.alpha"]},
            {**base, "tags": ["x" * 65]},
            {**base, "artifacts": ["sha256:bad"]},
            {**base, "relations": [{"type": "about", "target": "x" * 257}]},
            {**base, "relations": ""},
            {**base, "supersedes": 1},
        ]
        for note in invalid:
            with self.subTest(note=note), self.assertRaises(MemoryError):
                serialize_note(note)

    def test_invalid_filesystem_fixtures_quarantine_and_never_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            fixtures = Path(__file__).parent / "contracts" / "fixtures" / "memory"
            values = [
                json.loads((fixtures / "invalid-id.json").read_text(encoding="utf-8")),
                json.loads((fixtures / "invalid-relation.json").read_text(encoding="utf-8")),
            ]
            values.append({
                "schema": "a2a-superhub.memory.note.v1", "id": "mem_66666666666666666666666666666666",
                "type": "note", "title": "bad time", "author": "agent.alpha", "visibility": "shared",
                "recordedAt": "yesterday", "source": {"kind": "filesystem", "unexpected": "leak"}, "body": "must not index",
            })
            for index, value in enumerate(values):
                body = value.pop("body")
                raw = f"---\n{yaml.safe_dump(value, sort_keys=True)}---\n{body}".encode("utf-8")
                path = service.root / "notes" / "invalid" / f"{index}.md"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
            service.init()
            self.assertEqual(0, service.rebuild_index())
            self.assertEqual([], service.search("must not index", ADMIN))
            conn = sqlite3.connect(service.index_path)
            try:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0])
            finally:
                conn.close()
            self.assertEqual(3, len(service.quarantine()))

    def test_server_derives_author_and_enforces_create_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            result = service.create_note(self._request(), WRITER)
            self.assertEqual("agent.writer", result.note["author"])
            self.assertEqual("line one\nline two", result.note["body"])
            with self.assertRaises(MemoryError):
                service.create_note(self._request(author="agent.alpha"), WRITER)
            with self.assertRaises(AuthorizationError):
                service.create_note(self._request(visibility="shared"), WRITER)

    def test_idempotency_is_restart_safe_and_conflicts_on_changed_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(root)
            first = service.create_note(self._request(), OWNER, idempotency_key="idem-1")
            restarted = self._service(root)
            replay = restarted.create_note(self._request(body="line one\nline two"), OWNER, idempotency_key="idem-1")
            self.assertFalse(replay.inserted)
            self.assertEqual(first.note["id"], replay.note["id"])
            with self.assertRaises(ConflictError):
                restarted.create_note(self._request(title="changed"), OWNER, idempotency_key="idem-1")
            with self.assertRaises(MemoryError):
                restarted.create_note(self._request(), OWNER, idempotency_key="bad key with spaces")
            other = restarted.create_note(self._request(), WRITER, idempotency_key="idem-1")
            self.assertTrue(other.inserted)
            self.assertNotEqual(first.note["id"], other.note["id"])
            cli = restarted.create_note(self._request(), OWNER, idempotency_key="idem-1", source_kind="cli")
            self.assertTrue(cli.inserted)
            self.assertNotEqual(first.note["id"], cli.note["id"])

    def test_atomic_failpoints_and_startup_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self._service(root)
            with self.assertRaisesRegex(RuntimeError, "before_replace"):
                service.create_note(self._request(), OWNER, idempotency_key="before", failpoint="before_replace")
            self.assertEqual([], list((root / "memory" / "notes").glob("**/*.md")))
            retried = service.create_note(self._request(), OWNER, idempotency_key="before")
            self.assertTrue(retried.inserted)

            with self.assertRaisesRegex(RuntimeError, "after_replace_before_job"):
                service.create_note(self._request(title="crash window"), OWNER, idempotency_key="window", failpoint="after_replace_before_job")
            restarted = self._service(root)
            restarted.init()
            restarted.process_jobs()
            recovered = restarted.create_note(self._request(title="crash window"), OWNER, idempotency_key="window")
            self.assertFalse(recovered.inserted)
            self.assertEqual(recovered.note["id"], restarted.search("crash window", OWNER)[0]["id"])

    def test_final_authorization_uses_authoritative_markdown_not_stale_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            created = service.create_note(self._request(visibility="shared", body="secret marker"), OWNER)
            self.assertEqual(1, len(service.search("secret marker", OTHER)))
            changed = dict(created.note)
            changed["visibility"] = "private"
            atomic_write(note_path(service.root, changed["id"]), serialize_note(changed))
            stale = service.note_consistency(changed["id"])
            self.assertEqual(2, stale["sourceRevision"])
            self.assertEqual(1, stale["indexedRevision"])
            self.assertNotEqual(stale["sourceHash"], stale["indexedHash"])
            self.assertEqual(["index-stale"], service.index_status()["degraded"])
            self.assertEqual([], service.search("secret marker", OTHER))
            with self.assertRaises(KeyError):
                service.read_note(changed["id"], OTHER)
            self.assertEqual(changed["id"], service.read_note(changed["id"], OWNER)["id"])
            service.sync_filesystem()
            current = service.note_consistency(changed["id"])
            self.assertEqual(2, current["sourceRevision"])
            self.assertEqual(2, current["indexedRevision"])
            self.assertEqual(current["sourceHash"], current["indexedHash"])

    def test_rebuild_is_derived_and_does_not_mutate_ops_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            service.create_note(self._request(body="rebuild marker"), OWNER, idempotency_key="rebuild")
            before = service.ops_path.read_bytes()
            count = service.rebuild_index()
            after = service.ops_path.read_bytes()
            self.assertEqual(1, count)
            self.assertEqual(hashlib.sha256(before).digest(), hashlib.sha256(after).digest())
            self.assertEqual(1, len(service.search("rebuild marker", OWNER)))

    def test_partial_and_duplicate_paths_are_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            created = service.create_note(self._request(), OWNER)
            partial = service.root / "notes" / "ff" / "partial.md"
            partial.parent.mkdir(parents=True)
            partial.write_bytes(b"---\nid: unfinished")
            duplicate = service.root / "notes" / "ff" / "duplicate.md"
            duplicate.write_bytes(serialize_note(created.note))
            service.recover_jobs()
            paths = {item["path"] for item in service.quarantine()}
            self.assertIn("notes/ff/partial.md", {item.replace("\\", "/") for item in paths})
            self.assertIn("notes/ff/duplicate.md", {item.replace("\\", "/") for item in paths})

    def test_running_job_is_requeued_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            service.init()
            conn = sqlite3.connect(service.ops_path)
            try:
                conn.execute("INSERT INTO jobs VALUES ('index:missing:x', 'mem_99999999999999999999999999999999', 'running', 1, 't', 't')")
                conn.commit()
            finally:
                conn.close()
            restarted = self._service(Path(tmp))
            restarted.init()
            conn = sqlite3.connect(restarted.ops_path)
            try:
                state = conn.execute("SELECT state FROM jobs WHERE operation_id='index:missing:x'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("pending", state)

    def test_watcher_reindexes_rename_and_prunes_delete_without_changing_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = self._service(Path(tmp))
            created = service.create_note(self._request(body="watch marker"), OWNER)
            original = note_path(service.root, created.note["id"])
            renamed = service.root / "notes" / "renamed" / "observation.md"
            renamed.parent.mkdir(parents=True)
            original.rename(renamed)

            cycle = MemoryWatcher(service).scan_once()

            self.assertGreaterEqual(cycle["indexed"], 1)
            self.assertEqual(created.note["id"], service.read_note(created.note["id"], OWNER)["id"])
            self.assertEqual(created.note["id"], service.search("watch marker", OWNER)[0]["id"])
            conn = sqlite3.connect(service.index_path)
            try:
                relative = conn.execute("SELECT relative_path FROM manifest WHERE note_id=?", (created.note["id"],)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual("notes/renamed/observation.md", relative.replace("\\", "/"))

            renamed.unlink()
            removed = MemoryWatcher(service).scan_once()
            self.assertEqual(1, removed["removed"])
            self.assertEqual([], service.search("watch marker", OWNER))

    def test_ops_schema_forward_rollback_restore_and_forward_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            ops_path = state / "memory" / "ops.sqlite"
            ops_path.parent.mkdir(parents=True)
            conn = sqlite3.connect(ops_path)
            try:
                conn.execute("CREATE TABLE idempotency(key TEXT PRIMARY KEY, request_hash TEXT NOT NULL, note_id TEXT NOT NULL, created_at TEXT NOT NULL)")
                conn.execute("INSERT INTO idempotency VALUES ('legacy-key', 'hash', 'mem_11111111111111111111111111111111', 't')")
                conn.commit()
            finally:
                conn.close()
            backup_path = state / "ops-v0-backup.sqlite"
            source = sqlite3.connect(ops_path)
            backup = sqlite3.connect(backup_path)
            try:
                source.backup(backup)
            finally:
                source.close()
                backup.close()

            service = self._service(state)
            service.init()
            conn = sqlite3.connect(ops_path)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)")}
                migrated = conn.execute("SELECT principal, operation FROM idempotency WHERE key='legacy-key'").fetchone()
                version = conn.execute("PRAGMA user_version").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(("local.operator", "memory.note.create.api"), migrated)
            self.assertEqual(3, version)
            self.assertIn("principal", columns)
            self.assertIn("trace_id", columns)

            source = sqlite3.connect(backup_path)
            target = sqlite3.connect(ops_path)
            try:
                source.backup(target)
            finally:
                source.close()
                target.close()
            conn = sqlite3.connect(ops_path)
            try:
                rolled_back = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)")}
            finally:
                conn.close()
            self.assertNotIn("principal", rolled_back)

            service.init()
            conn = sqlite3.connect(ops_path)
            try:
                forwarded_again = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)")}
                row_count = conn.execute("SELECT COUNT(*) FROM idempotency WHERE key='legacy-key'").fetchone()[0]
                version_again = conn.execute("PRAGMA user_version").fetchone()[0]
                sharing_tables = {
                    row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    if row[0] in {"deliveries", "consumer_cursors", "issued_cursors", "receipts"}
                }
            finally:
                conn.close()
            self.assertIn("principal", forwarded_again)
            self.assertIn("trace_id", forwarded_again)
            self.assertEqual(1, row_count)
            self.assertEqual(3, version_again)
            self.assertEqual({"deliveries", "consumer_cursors", "issued_cursors", "receipts"}, sharing_tables)


if __name__ == "__main__":
    unittest.main()
