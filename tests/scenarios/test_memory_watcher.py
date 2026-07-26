from __future__ import annotations

import builtins
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from a2a_superhub.auth import Principal
from a2a_superhub.client import HubClient
from a2a_superhub.memory import MemoryService, MemoryWatcher, QuarantineError, note_path, parse_note, path_collision_key, serialize_note, validate_existing_path
from a2a_superhub.server import make_server
import yaml


OWNER = Principal("agent.alpha", "agent", "tok_owner", frozenset({"memory.read", "memory.write", "memory.share"}))
ADMIN = Principal("local.operator", "operator", "tok_admin", frozenset({"memory.read", "memory.write", "memory.share", "memory.admin"}))


class MemoryWatcherScenarios(unittest.TestCase):
    @staticmethod
    def _write_retry_sentinel(service: MemoryService, note_id: str) -> None:
        note = {
            "schema": "a2a-superhub.memory.note.v1",
            "id": note_id,
            "type": "observation",
            "title": "runtime retry sentinel",
            "author": "local.operator",
            "visibility": "shared",
            "recordedAt": "2026-07-26T13:00:00Z",
            "source": {"kind": "filesystem"},
            "body": "RUNTIME-WATCHER-RETRY-SENTINEL",
        }
        path = note_path(service.root, note_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(serialize_note(note))

    def test_keyword_search_reads_committed_snapshot_while_index_writer_is_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp))
            created = service.create_note(
                {
                    "type": "observation",
                    "title": "WAL search sentinel",
                    "visibility": "shared",
                    "body": "WAL-READS-LAST-COMMITTED-SNAPSHOT",
                },
                OWNER,
                idempotency_key="wal-search-sentinel",
            )
            legacy = sqlite3.connect(service.index_path)
            try:
                legacy_mode = str(
                    legacy.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                )
            finally:
                legacy.close()
            self.assertEqual("delete", legacy_mode.casefold())

            service = MemoryService(Path(tmp))
            service.init()
            writer = sqlite3.connect(service.index_path, isolation_level=None)
            writer.execute("PRAGMA busy_timeout=5000")
            writer.execute("BEGIN EXCLUSIVE")
            writer.execute(
                "UPDATE notes SET body='uncommitted replacement' WHERE note_id=?",
                (created.note["id"],),
            )
            search_finished = threading.Event()
            search_results: list[list[dict]] = []
            search_failures: list[BaseException] = []

            def search() -> None:
                try:
                    search_results.append(
                        service.search("WAL-READS-LAST-COMMITTED-SNAPSHOT", OWNER)
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    search_failures.append(exc)
                finally:
                    search_finished.set()

            search_thread = threading.Thread(target=search, name="wal-concurrent-reader")
            search_thread.start()
            completed_while_writer_was_open = search_finished.wait(1)
            writer.rollback()
            writer.close()
            search_thread.join(timeout=10)

            journal = sqlite3.connect(service.index_path)
            try:
                journal_mode = str(journal.execute("PRAGMA journal_mode").fetchone()[0])
            finally:
                journal.close()

            self.assertTrue(
                completed_while_writer_was_open,
                "keyword search waited for an uncommitted derived-index writer",
            )
            self.assertFalse(search_thread.is_alive())
            self.assertEqual([], search_failures)
            self.assertEqual(created.note["id"], search_results[0][0]["id"])
            self.assertEqual("wal", journal_mode.casefold())

    def test_search_remains_available_while_convergence_scans_the_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), enable_delivery=True)
            created = service.create_note(
                {
                    "type": "observation",
                    "title": "search availability sentinel",
                    "visibility": "shared",
                    "body": "SEARCH-AVAILABLE-DURING-SCAN",
                },
                OWNER,
                idempotency_key="search-availability-sentinel",
            )
            service.init()

            scan_entered = threading.Event()
            release_scan = threading.Event()
            search_finished = threading.Event()
            convergence_failures: list[BaseException] = []
            search_failures: list[BaseException] = []
            search_results: list[list[dict]] = []
            original_signature = service._file_signature

            def blocked_signature(path):
                if threading.current_thread().name == "blocked-convergence" and not scan_entered.is_set():
                    scan_entered.set()
                    if not release_scan.wait(5):
                        raise AssertionError("test did not release the blocked convergence scan")
                return original_signature(path)

            def converge() -> None:
                try:
                    service.sync_filesystem()
                except BaseException as exc:  # pragma: no cover - asserted below
                    convergence_failures.append(exc)

            def search() -> None:
                try:
                    search_results.append(service.search("SEARCH-AVAILABLE-DURING-SCAN", OWNER))
                except BaseException as exc:  # pragma: no cover - asserted below
                    search_failures.append(exc)
                finally:
                    search_finished.set()

            with patch.object(service, "_file_signature", side_effect=blocked_signature):
                convergence_thread = threading.Thread(target=converge, name="blocked-convergence")
                convergence_thread.start()
                self.assertTrue(scan_entered.wait(5), "convergence did not enter the corpus scan")
                search_thread = threading.Thread(target=search, name="concurrent-search")
                search_thread.start()
                completed_while_scan_was_blocked = search_finished.wait(1)
                release_scan.set()
                convergence_thread.join(timeout=10)
                search_thread.join(timeout=10)

            self.assertTrue(completed_while_scan_was_blocked, "search waited for the full convergence scan")
            self.assertFalse(convergence_thread.is_alive())
            self.assertFalse(search_thread.is_alive())
            self.assertEqual([], convergence_failures)
            self.assertEqual([], search_failures)
            self.assertEqual(created.note["id"], search_results[0][0]["id"])

    def test_http_search_remains_available_while_filesystem_convergence_reads_slow_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            principals = {
                "owner-token": {
                    "subject": "agent.alpha",
                    "kind": "agent",
                    "tokenId": "tok_http_owner",
                    "scopes": ["memory.read", "memory.write", "memory.share"],
                },
                "admin-token": {
                    "subject": "local.operator",
                    "kind": "operator",
                    "tokenId": "tok_http_admin",
                    "scopes": ["hub.admin"],
                },
            }
            server = make_server(
                Path(tmp),
                host="127.0.0.1",
                port=0,
                principals=principals,
                enable_memory=True,
                enable_delivery=True,
            )
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            client = HubClient(
                f"http://127.0.0.1:{server.server_port}",
                token="owner-token",
                timeout=5,
            )
            admin = HubClient(
                f"http://127.0.0.1:{server.server_port}",
                token="admin-token",
                timeout=5,
            )
            service = server.memory_service
            self.assertIsNotNone(service)
            created = client.create_note(
                {
                    "type": "observation",
                    "title": "HTTP search availability sentinel",
                    "visibility": "shared",
                    "body": "HTTP-SEARCH-AVAILABLE-DURING-SCAN",
                },
                "http-search-availability-sentinel",
            )
            filesystem_id = "mem_00000000000000000000000000000003"
            filesystem_note = {
                "schema": "a2a-superhub.memory.note.v1",
                "id": filesystem_id,
                "type": "observation",
                "title": "slow filesystem addition",
                "author": "local.operator",
                "visibility": "shared",
                "recordedAt": "2026-07-25T00:00:00Z",
                "source": {"kind": "filesystem"},
                "body": "filesystem convergence payload",
            }
            path = note_path(service.root, filesystem_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(serialize_note(filesystem_note))

            scan_entered = threading.Event()
            release_scan = threading.Event()
            search_read_entered = threading.Event()
            search_finished = threading.Event()
            diagnostics_finished = threading.Event()
            convergence_failures: list[BaseException] = []
            search_failures: list[BaseException] = []
            diagnostics_failures: list[BaseException] = []
            search_results: list[dict] = []
            diagnostics_results: list[dict] = []
            original_signature = service._file_signature

            def blocked_signature(candidate):
                if threading.current_thread().name == "http-blocked-convergence" and not scan_entered.is_set():
                    scan_entered.set()
                    if not release_scan.wait(5):
                        raise AssertionError("test did not release the blocked HTTP convergence scan")
                elif scan_entered.is_set() and not release_scan.is_set():
                    search_read_entered.set()
                return original_signature(candidate)

            def converge() -> None:
                try:
                    service.sync_filesystem()
                except BaseException as exc:  # pragma: no cover - asserted below
                    convergence_failures.append(exc)

            def search() -> None:
                try:
                    search_results.append(client.search("HTTP-SEARCH-AVAILABLE-DURING-SCAN"))
                except BaseException as exc:  # pragma: no cover - asserted below
                    search_failures.append(exc)
                finally:
                    search_finished.set()

            def diagnose() -> None:
                try:
                    diagnostics_results.append(
                        admin.request("GET", "/v1/operations/diagnostics")
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    diagnostics_failures.append(exc)
                finally:
                    diagnostics_finished.set()

            try:
                with patch.object(service, "_file_signature", side_effect=blocked_signature):
                    convergence_thread = threading.Thread(target=converge, name="http-blocked-convergence")
                    convergence_thread.start()
                    self.assertTrue(scan_entered.wait(5), "HTTP convergence did not enter the corpus scan")
                    search_thread = threading.Thread(target=search, name="http-concurrent-search")
                    diagnostics_thread = threading.Thread(target=diagnose, name="http-concurrent-diagnostics")
                    search_thread.start()
                    diagnostics_thread.start()
                    search_read_while_scan_was_blocked = search_read_entered.wait(1)
                    completed_while_scan_was_blocked = search_finished.wait(3)
                    diagnostics_completed_while_scan_was_blocked = diagnostics_finished.wait(3)
                    release_scan.set()
                    convergence_thread.join(timeout=10)
                    search_thread.join(timeout=10)
                    diagnostics_thread.join(timeout=10)
            finally:
                release_scan.set()
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=5)

            self.assertTrue(
                search_read_while_scan_was_blocked,
                "real HTTP keyword search could not read authoritative state during filesystem convergence",
            )
            self.assertTrue(
                completed_while_scan_was_blocked,
                "real HTTP keyword search waited for filesystem convergence",
            )
            self.assertTrue(
                diagnostics_completed_while_scan_was_blocked,
                "payload-free diagnostics waited for filesystem convergence",
            )
            self.assertFalse(convergence_thread.is_alive())
            self.assertFalse(search_thread.is_alive())
            self.assertFalse(diagnostics_thread.is_alive())
            self.assertEqual([], convergence_failures)
            self.assertEqual([], search_failures)
            self.assertEqual([], diagnostics_failures)
            self.assertEqual(created["id"], search_results[0]["items"][0]["id"])
            self.assertGreaterEqual(
                search_results[0]["sourceRevision"],
                search_results[0]["items"][0]["sourceRevision"],
                "completed index status lagged behind a completed API write",
            )
            self.assertEqual(0, diagnostics_results[0]["stores"]["memory"]["index"]["lagRecords"])

    def test_convergence_does_not_hold_writer_lock_while_planning_unchanged_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), enable_delivery=True)
            for index in range(24):
                service.create_note(
                    {
                        "type": "observation",
                        "title": f"existing {index}",
                        "visibility": "shared",
                        "body": "already indexed",
                    },
                    OWNER,
                    idempotency_key=f"existing-{index}",
                )
            filesystem_id = "mem_00000000000000000000000000000001"
            filesystem_note = {
                "schema": "a2a-superhub.memory.note.v1",
                "id": filesystem_id,
                "type": "observation",
                "title": "filesystem addition",
                "author": "local.operator",
                "visibility": "shared",
                "recordedAt": "2026-07-21T00:00:00Z",
                "source": {"kind": "filesystem"},
                "body": "new authoritative note",
            }
            path = note_path(service.root, filesystem_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(serialize_note(filesystem_note))

            original_job_operation = service._job_operation
            planned = 0
            planning_started = threading.Event()

            def slow_job_operation(note, note_file):
                nonlocal planned
                operation = original_job_operation(note, note_file)
                planned += 1
                if planned == 2:
                    planning_started.set()
                time.sleep(0.01)
                return operation

            failure: list[BaseException] = []

            def converge() -> None:
                try:
                    service.sync_filesystem()
                except BaseException as exc:  # pragma: no cover - asserted below
                    failure.append(exc)

            with patch.object(service, "_job_operation", side_effect=slow_job_operation):
                worker = threading.Thread(target=converge)
                worker.start()
                self.assertTrue(planning_started.wait(5), "convergence did not reach job planning")
                competing = sqlite3.connect(service.ops_path, timeout=0.05)
                try:
                    competing.execute("PRAGMA busy_timeout=50")
                    competing.execute(
                        "INSERT INTO ops_metadata(key,value) VALUES('concurrent-probe','ok') "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
                    )
                    competing.commit()
                finally:
                    competing.close()
                worker.join(timeout=10)

            self.assertFalse(worker.is_alive())
            self.assertEqual([], failure)

    def test_convergence_generates_deliveries_only_for_changed_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(Path(tmp), enable_delivery=True)
            for index in range(12):
                service.create_note(
                    {
                        "type": "handoff",
                        "title": f"existing handoff {index}",
                        "visibility": "direct:agent.beta",
                        "body": "already delivered",
                    },
                    OWNER,
                    idempotency_key=f"existing-handoff-{index}",
                )
            filesystem_id = "mem_00000000000000000000000000000002"
            filesystem_note = {
                "schema": "a2a-superhub.memory.note.v1",
                "id": filesystem_id,
                "type": "handoff",
                "title": "new filesystem handoff",
                "author": "local.operator",
                "visibility": "direct:agent.beta",
                "recordedAt": "2026-07-21T00:00:00Z",
                "source": {"kind": "filesystem"},
                "body": "deliver only this change",
            }
            path = note_path(service.root, filesystem_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(serialize_note(filesystem_note))

            generated: list[str] = []
            original_generate = service._generate_deliveries_for_note

            def counted_generate(note, **kwargs):
                generated.append(note["id"])
                return original_generate(note, **kwargs)

            with patch.object(service, "_generate_deliveries_for_note", side_effect=counted_generate):
                service.sync_filesystem()

            self.assertEqual([filesystem_id], generated)

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

    def test_failed_watcher_flush_retains_the_pending_generation_for_retry(self) -> None:
        attempts = 0

        class FlakyService:
            def sync_filesystem(self) -> dict[str, int]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("simulated transient contention")
                return {"assigned": 0, "enqueued": 1, "indexed": 1, "removed": 0}

        watcher = MemoryWatcher(FlakyService(), clock=lambda: 100.0, debounce_seconds=0)
        watcher.notify("notes/retry.md", "modified")

        with self.assertRaises(sqlite3.OperationalError):
            watcher.flush(force=True)
        self.assertEqual(1, watcher.pending_count)

        self.assertEqual(
            {"assigned": 0, "enqueued": 1, "indexed": 1, "removed": 0},
            watcher.flush(force=True),
        )
        self.assertEqual(0, watcher.pending_count)
        self.assertEqual(2, attempts)

    def test_runtime_watchdog_retries_failed_generation_without_a_second_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            server = make_server(Path(tmp), host="127.0.0.1", port=0, enable_memory=True)
            service = server.memory_service
            self.assertIsNotNone(service)
            service.init()
            convergence = server.memory_convergence_event
            convergence.clear()
            original_sync = service.sync_filesystem
            attempts = 0

            def fail_once() -> dict[str, int]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("simulated watcher contention")
                return original_sync()

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(service, "sync_filesystem", side_effect=fail_once):
                    self._write_retry_sentinel(
                        service,
                        "mem_11111111111111111111111111111111",
                    )
                    self.assertTrue(
                        convergence.wait(8),
                        "watchdog did not retry the retained generation",
                    )
                self.assertGreaterEqual(attempts, 2)
                self.assertEqual(
                    1,
                    len(service.search("RUNTIME-WATCHER-RETRY-SENTINEL", ADMIN)),
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_polling_fallback_retries_failed_unchanged_snapshot(self) -> None:
        real_import = builtins.__import__

        def without_watchdog(name: str, *args: object, **kwargs: object):
            if name == "watchdog.events" or name == "watchdog.observers":
                raise ImportError("simulated optional watchdog absence")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with patch("builtins.__import__", side_effect=without_watchdog):
                server = make_server(Path(tmp), host="127.0.0.1", port=0, enable_memory=True)
            service = server.memory_service
            self.assertIsNotNone(service)
            service.init()
            convergence = server.memory_convergence_event
            convergence.clear()
            original_sync = service.sync_filesystem
            attempts = 0

            def fail_once() -> dict[str, int]:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sqlite3.OperationalError("simulated polling contention")
                return original_sync()

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(service, "sync_filesystem", side_effect=fail_once):
                    self._write_retry_sentinel(
                        service,
                        "mem_22222222222222222222222222222222",
                    )
                    self.assertTrue(
                        convergence.wait(8),
                        "polling fallback did not retry the unchanged snapshot",
                    )
                self.assertGreaterEqual(attempts, 2)
                self.assertEqual(
                    1,
                    len(service.search("RUNTIME-WATCHER-RETRY-SENTINEL", ADMIN)),
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_api_create_and_filesystem_convergence_have_one_mutation_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = MemoryService(
                Path(tmp),
                enable_delivery=True,
                enable_watcher_side_effects=True,
            )
            service.init()
            external_id = "mem_ffffffffffffffffffffffffffffffff"
            external = {
                "schema": "a2a-superhub.memory.note.v1",
                "id": external_id,
                "type": "observation",
                "title": "filesystem concurrency sentinel",
                "author": "local.operator",
                "visibility": "shared",
                "recordedAt": "2026-07-26T13:00:00Z",
                "source": {"kind": "filesystem"},
                "about": ["agent.beta"],
                "body": "FILESYSTEM-ONLY-SENTINEL",
            }
            external_path = note_path(service.root, external_id)
            external_path.parent.mkdir(parents=True, exist_ok=True)
            external_path.write_bytes(serialize_note(external))

            original_upsert = service._upsert_index
            first_upsert_entered = threading.Event()
            release_first_upsert = threading.Event()
            active_lock = threading.Lock()
            active = 0
            max_active = 0
            failures: list[BaseException] = []

            def observed_upsert(note: dict[str, object], **kwargs: object) -> None:
                nonlocal active, max_active
                with active_lock:
                    active += 1
                    max_active = max(max_active, active)
                try:
                    if threading.current_thread().name == "api-create":
                        first_upsert_entered.set()
                        if not release_first_upsert.wait(5):
                            raise AssertionError("test did not release API index mutation")
                    original_upsert(note, **kwargs)
                finally:
                    with active_lock:
                        active -= 1

            service._upsert_index = observed_upsert  # type: ignore[method-assign]

            def create_api_note() -> None:
                try:
                    service.create_note(
                        {
                            "type": "observation",
                            "title": "API concurrency sentinel",
                            "visibility": "shared",
                            "about": ["agent.beta"],
                            "body": "API-CONVERGENCE-OWNER-SENTINEL",
                        },
                        OWNER,
                        idempotency_key="convergence-owner",
                    )
                except BaseException as exc:
                    failures.append(exc)

            def converge_filesystem() -> None:
                try:
                    service.sync_filesystem()
                except BaseException as exc:
                    failures.append(exc)

            create_thread = threading.Thread(target=create_api_note, name="api-create")
            convergence_thread = threading.Thread(
                target=converge_filesystem,
                name="filesystem-convergence",
            )
            create_thread.start()
            self.assertTrue(first_upsert_entered.wait(5), "API create did not reach index mutation")
            convergence_thread.start()
            time.sleep(0.2)
            with active_lock:
                self.assertEqual(1, max_active)
            self.assertTrue(convergence_thread.is_alive())
            release_first_upsert.set()
            create_thread.join(timeout=10)
            convergence_thread.join(timeout=10)

            self.assertFalse(create_thread.is_alive())
            self.assertFalse(convergence_thread.is_alive())
            self.assertEqual([], failures)
            with active_lock:
                self.assertEqual(1, max_active)
            self.assertEqual(
                [external_id],
                [item["id"] for item in service.search("FILESYSTEM-ONLY-SENTINEL", ADMIN)],
            )

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
