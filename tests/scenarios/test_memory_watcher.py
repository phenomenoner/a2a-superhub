from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService, MemoryWatcher, QuarantineError, note_path, parse_note, path_collision_key, serialize_note, validate_existing_path
import yaml


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))
ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"memory.read", "memory.write", "memory.share", "memory.admin"}))


class MemoryWatcherScenarios(unittest.TestCase):
    def test_duplicate_quarantines_both_then_removal_recovers_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), now=lambda: "2026-07-19T12:00:00Z", new_note_id=lambda: "mem_dddddddddddddddddddddddddddddddd")
            created = service.create_note(
                {"type": "note", "title": "collision", "visibility": "private", "body": "must disappear"},
                OWNER,
                idempotency_key="collision",
            )
            canonical = next((service.root / "notes").glob("**/*.md"))
            duplicate = service.root / "notes" / "renamed" / "copy.md"
            duplicate.parent.mkdir(parents=True)
            shutil.copyfile(canonical, duplicate)

            MemoryWatcher(service).scan_once()

            self.assertEqual(2, service.stats(ADMIN)["quarantineCount"])

            with self.assertRaises(QuarantineError):
                service.read_note(created.note["id"], OWNER)
            self.assertEqual([], service.search("must disappear", OWNER))
            conn = sqlite3.connect(service.index_path)
            try:
                indexed = conn.execute("SELECT COUNT(*) FROM manifest WHERE note_id=?", (created.note["id"],)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(0, indexed)
            quarantined = {item["path"].replace("\\", "/") for item in service.quarantine()}
            self.assertIn(str(canonical.relative_to(service.root)).replace("\\", "/"), quarantined)
            self.assertIn("notes/renamed/copy.md", quarantined)

            duplicate.unlink()
            MemoryWatcher(service).scan_once()

            self.assertEqual(created.note["id"], service.read_note(created.note["id"], OWNER)["id"])
            self.assertEqual(1, len(service.search("must disappear", OWNER)))
            self.assertEqual(0, service.stats(ADMIN)["quarantineCount"])
            self.assertTrue(all(item["state"] == "resolved" for item in service.quarantine()))

    def test_full_rebuild_excludes_every_member_of_duplicate_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), now=lambda: "2026-07-19T12:00:00Z", new_note_id=lambda: "mem_cccccccccccccccccccccccccccccccc")
            created = service.create_note(
                {"type": "note", "title": "rebuild collision", "visibility": "private", "body": "never index duplicate"},
                OWNER,
                idempotency_key="rebuild-collision",
            )
            canonical = next((service.root / "notes").glob("**/*.md"))
            duplicate = service.root / "notes" / "duplicate" / "copy.md"
            duplicate.parent.mkdir(parents=True)
            shutil.copyfile(canonical, duplicate)

            self.assertEqual(0, service.rebuild_index())

            with self.assertRaises(QuarantineError):
                service.read_note(created.note["id"], OWNER)
            self.assertEqual([], service.search("never index duplicate", OWNER))
            conn = sqlite3.connect(service.index_path)
            try:
                self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0])
            finally:
                conn.close()

    def test_windows_semantic_path_keys_casefold_and_reject_non_nfc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "memory"
            upper = root / "notes" / "Topic" / "Note.md"
            lower = root / "notes" / "topic" / "note.md"
            self.assertEqual(path_collision_key(root, upper), path_collision_key(root, lower))
            decomposed = root / "notes" / "unicode" / "e\u0301.md"
            with self.assertRaises(QuarantineError):
                validate_existing_path(root, decomposed)

    def test_watchdog_callback_debounces_partial_edit_rename_delete_and_restart_scan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            now = [100.0]
            service = MemoryService(Path(tmp), now=lambda: "2026-07-19T12:00:00Z", new_note_id=lambda: "mem_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee")
            created = service.create_note(
                {"type": "note", "title": "debounce", "visibility": "private", "body": "before"},
                OWNER,
                idempotency_key="debounce",
            )
            path = note_path(service.root, created.note["id"])
            watcher = MemoryWatcher(service, clock=lambda: now[0], debounce_seconds=1.0)
            path.write_bytes(b"---\npartial")
            watcher.on_any_event(SimpleNamespace(src_path=str(path), event_type="modified", is_directory=False))
            note = dict(created.note)
            note["body"] = "after burst"
            path.write_bytes(serialize_note(note))
            watcher.on_any_event(SimpleNamespace(src_path=str(path), event_type="modified", is_directory=False))
            self.assertEqual(1, watcher.pending_count)
            self.assertIsNone(watcher.flush())
            now[0] += 1.0
            self.assertIsNotNone(watcher.flush())
            self.assertEqual(1, len(service.search("after burst", OWNER)))

            renamed = service.root / "notes" / "moved" / "note.md"
            renamed.parent.mkdir(parents=True)
            path.rename(renamed)
            watcher.on_any_event(SimpleNamespace(src_path=str(path), dest_path=str(renamed), event_type="moved", is_directory=False))
            now[0] += 1.0
            watcher.flush()
            self.assertEqual(created.note["id"], service.read_note(created.note["id"], OWNER)["id"])

            renamed.unlink()
            watcher.on_any_event(SimpleNamespace(src_path=str(renamed), event_type="deleted", is_directory=False))
            now[0] += 1.0
            watcher.flush()
            self.assertEqual([], service.search("after burst", OWNER))

            restored = service.root / "notes" / "restored" / "note.md"
            restored.parent.mkdir(parents=True)
            restored.write_bytes(serialize_note(note))
            restarted_watcher = MemoryWatcher(service, clock=lambda: now[0], debounce_seconds=1.0)
            restarted_watcher.startup_scan()
            self.assertEqual(1, len(service.search("after burst", OWNER)))

    def test_local_admin_missing_id_assignment_is_flagged_atomic_and_collision_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            metadata = {
                "schema": "a2a-superhub.memory.note.v1", "type": "note", "title": "human edit",
                "author": "local.operator", "visibility": "private", "recordedAt": "2026-07-19T13:00:00Z",
                "source": {"kind": "filesystem"},
            }
            raw = f"---\n{yaml.safe_dump(metadata, sort_keys=True)}---\nlocal body".encode("utf-8")
            first = state / "memory" / "notes" / "human" / "first.md"
            second = state / "memory" / "notes" / "human" / "second.md"
            first.parent.mkdir(parents=True)
            first.write_bytes(raw)
            second.write_bytes(raw)

            disabled = MemoryService(state)
            disabled.sync_filesystem()
            self.assertEqual(raw, first.read_bytes())

            enabled = MemoryService(state, enable_watcher_side_effects=True)
            result = enabled.sync_filesystem()
            parsed_first = parse_note(first.read_bytes())
            parsed_second = parse_note(second.read_bytes())
            self.assertEqual(2, result["assigned"])
            self.assertNotEqual(parsed_first["id"], parsed_second["id"])
            self.assertEqual("local body", parsed_first["body"])
            self.assertEqual(2, len(enabled.search("local body", ADMIN)))

            remote = state / "memory" / "notes" / "human" / "remote.md"
            remote_metadata = dict(metadata)
            remote_metadata["author"] = "agent.alpha"
            remote.write_bytes(f"---\n{yaml.safe_dump(remote_metadata, sort_keys=True)}---\nremote body".encode("utf-8"))
            enabled.sync_filesystem()
            self.assertEqual("agent.alpha", yaml.safe_load(remote.read_text(encoding="utf-8").split("---\n")[1])["author"])
            self.assertNotIn("\nid:", remote.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
