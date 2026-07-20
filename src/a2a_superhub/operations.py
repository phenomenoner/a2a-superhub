from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

from . import __version__
from .artifacts import ArtifactStore
from .auth import Principal
from .memory import MemoryService, parse_note
from .store import HubStore


BACKUP_SCHEMA = "a2a-superhub.backup.v1"
DIAGNOSTICS_SCHEMA = "a2a-superhub.operations-diagnostics.v1"
SEARCH_PROVIDER_SCHEMA = "a2a-superhub.search-provider.v1"


class OperationsError(RuntimeError):
    pass


class BackupSafetyError(OperationsError):
    pass


class RestoreSafetyError(OperationsError):
    pass


class RetentionBlockedError(OperationsError):
    pass


class MigrationError(OperationsError):
    pass


def load_search_provider_config(state_dir: str | Path) -> dict[str, Any]:
    """Load the activated provider without silently accepting malformed state."""
    path = Path(state_dir) / "retrieval" / "provider.json"
    if not path.is_file():
        raise MigrationError("no activated search provider configuration exists")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationError("activated search provider configuration is invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != SEARCH_PROVIDER_SCHEMA:
        raise MigrationError("activated search provider configuration has an unsupported schema")
    mode = value.get("mode")
    if mode not in {"keyword", "local", "server"}:
        raise MigrationError("activated search provider mode is invalid")
    if mode == "server" and not isinstance(value.get("url"), str):
        raise MigrationError("activated server search provider is missing its URL")
    return {"mode": mode, "url": value.get("url")}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _within(root: Path, path: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise OperationsError("operation path escapes the state root") from exc
    return resolved


class StateLease:
    """Cross-process exclusive lease for state-wide operational mutations."""

    def __init__(self, state_dir: str | Path, *, purpose: str):
        self.state_dir = Path(state_dir)
        self.purpose = purpose
        self.path = self.state_dir / ".operations.lock"
        self._handle = None

    def __enter__(self) -> "StateLease":
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._handle.close()
            self._handle = None
            raise OperationsError(f"hub state is active; cannot acquire {self.purpose} lease") from exc
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


class BackupManager:
    """Manifested, integrity-checked backup of authoritative hub state."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).resolve()

    @staticmethod
    def _classification(relative: str, source: Path) -> str:
        if relative == "config/principals.json":
            return "secret"
        if relative == "tasks/hub-tasks.sqlite":
            return "private"
        if relative.startswith("memory/notes/"):
            try:
                visibility = parse_note(source.read_bytes()).get("visibility")
                return "shared" if visibility == "shared" else "private"
            except Exception:
                return "private"
        if relative.startswith("artifacts/manifests/"):
            try:
                visibility = json.loads(source.read_text(encoding="utf-8")).get("visibility")
                return "shared" if visibility == "shared" else "private"
            except Exception:
                return "private"
        if relative.startswith("artifacts/blobs/"):
            return "private"
        if relative.endswith("ops.sqlite") or relative.startswith("operations/"):
            return "private"
        return "operational"

    @staticmethod
    def _copy_file(source: Path, destination: Path) -> None:
        if source.is_symlink():
            raise BackupSafetyError("backup refuses symlinked authoritative files")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def _stage_authoritative(self, stage: Path, auth_config: Path | None) -> list[dict[str, Any]]:
        candidates: list[tuple[Path, str]] = []
        sqlite_files = (
            (self.state_dir / "tasks" / "hub-tasks.sqlite", "tasks/hub-tasks.sqlite"),
            (self.state_dir / "memory" / "ops.sqlite", "memory/ops.sqlite"),
            (self.state_dir / "operations" / "operations.sqlite", "operations/operations.sqlite"),
        )
        for source, relative in sqlite_files:
            if source.is_file():
                destination = stage / relative
                _sqlite_snapshot(source, destination)
                candidates.append((destination, relative))

        roots = (
            self.state_dir / "memory" / "notes",
            self.state_dir / "artifacts" / "manifests",
            self.state_dir / "artifacts" / "blobs",
            self.state_dir / "artifacts" / "uploads",
            self.state_dir / "artifacts" / "temp" / "uploads",
            self.state_dir / "trash",
        )
        for root in roots:
            if not root.exists():
                continue
            if root.is_symlink():
                raise BackupSafetyError("backup refuses symlinked authoritative roots")
            for source in sorted(path for path in root.rglob("*") if path.is_file()):
                relative = source.relative_to(self.state_dir).as_posix()
                destination = stage / relative
                self._copy_file(source, destination)
                candidates.append((destination, relative))

        if auth_config is not None:
            source = auth_config.resolve()
            if not source.is_file() or source.is_symlink():
                raise BackupSafetyError("auth config must be a regular file")
            try:
                parsed = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BackupSafetyError("auth config must contain valid JSON") from exc
            if not isinstance(parsed, dict):
                raise BackupSafetyError("auth config must contain a principal registry object")
            destination = stage / "config" / "principals.json"
            self._copy_file(source, destination)
            candidates.append((destination, "config/principals.json"))

        entries = []
        for source, relative in sorted(candidates, key=lambda item: item[1]):
            entries.append({
                "path": relative,
                "sha256": _sha256(source),
                "bytes": source.stat().st_size,
                "classification": self._classification(relative, source),
            })
        return entries

    def create(
        self,
        destination: str | Path,
        *,
        auth_config: str | Path | None = None,
        target_class: str = "private",
        allow_sensitive_public: bool = False,
    ) -> dict[str, Any]:
        if target_class not in {"private", "public"}:
            raise BackupSafetyError("backup target class must be private or public")
        destination = Path(destination).resolve()
        if destination.exists():
            raise BackupSafetyError("backup destination already exists")
        if self.state_dir == destination or self.state_dir in destination.parents:
            raise BackupSafetyError("backup destination must be outside the state root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            lease = StateLease(self.state_dir, purpose="backup")
            lease.__enter__()
        except OperationsError as exc:
            raise BackupSafetyError(str(exc)) from exc
        try:
            with tempfile.TemporaryDirectory(prefix="a2a-superhub-backup-") as temporary:
                stage = Path(temporary) / "payload"
                stage.mkdir()
                entries = self._stage_authoritative(stage, Path(auth_config) if auth_config else None)
                sensitive = any(entry["classification"] in {"private", "secret"} for entry in entries)
                if target_class == "public" and sensitive and not allow_sensitive_public:
                    raise BackupSafetyError("public backup target refuses sensitive authoritative state")
                warnings = []
                if target_class == "public" and sensitive:
                    warnings.append("explicit override includes sensitive state in a public-classified backup")
                manifest = {
                    "schema": BACKUP_SCHEMA,
                    "productVersion": __version__,
                    "createdAt": _now(),
                    "targetClass": target_class,
                    "containsSensitive": sensitive,
                    "overrideRecorded": bool(target_class == "public" and sensitive and allow_sensitive_public),
                    "warnings": warnings,
                    "files": entries,
                    "excludedDerived": ["memory/index.sqlite", "retrieval/qdrant", "retrieval/active.json", "retrieval/rebuild.json"],
                }
                _atomic_json(stage / "manifest.json", manifest)
                temporary_archive = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
                try:
                    with zipfile.ZipFile(temporary_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                        archive.write(stage / "manifest.json", "manifest.json")
                        for entry in entries:
                            archive.write(stage / entry["path"], entry["path"])
                    os.replace(temporary_archive, destination)
                finally:
                    temporary_archive.unlink(missing_ok=True)
                result = dict(manifest)
                result.pop("files")
                result["fileCount"] = len(entries)
                result["archiveSha256"] = _sha256(destination)
                return result
        finally:
            lease.__exit__(None, None, None)

    @staticmethod
    def restore(source: str | Path, target_state: str | Path) -> dict[str, Any]:
        source = Path(source).resolve()
        target = Path(target_state).resolve()
        if not source.is_file():
            raise RestoreSafetyError("backup archive does not exist")
        if target.exists():
            raise RestoreSafetyError("restore target must not exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as archive:
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (KeyError, ValueError) as exc:
                raise RestoreSafetyError("backup manifest is missing or invalid") from exc
            if manifest.get("schema") != BACKUP_SCHEMA or not isinstance(manifest.get("files"), list):
                raise RestoreSafetyError("unsupported backup manifest")
            entries = manifest["files"]
            if len(entries) > 1_000_000:
                raise RestoreSafetyError("backup manifest contains too many files")
            for entry in entries:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str)
                    or not isinstance(entry.get("bytes"), int)
                    or entry["bytes"] < 0
                    or not isinstance(entry.get("sha256"), str)
                    or len(entry["sha256"]) != 64
                ):
                    raise RestoreSafetyError("backup manifest contains an invalid file entry")
            expected = {"manifest.json", *(entry["path"] for entry in entries)}
            name_list = archive.namelist()
            names = set(name_list)
            if len(name_list) != len(names) or names != expected:
                raise RestoreSafetyError("backup archive contents do not match its manifest")
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts or "" in pure.parts:
                    raise RestoreSafetyError("backup archive contains an unsafe path")
            with tempfile.TemporaryDirectory(prefix=".a2a-superhub-restore-", dir=target.parent) as temporary:
                staged = Path(temporary) / "state"
                staged.mkdir()
                for entry in entries:
                    if archive.getinfo(entry["path"]).file_size != entry["bytes"]:
                        raise RestoreSafetyError("backup member size does not match its manifest")
                    destination = staged.joinpath(*PurePosixPath(entry["path"]).parts)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(entry["path"]) as reader, destination.open("xb") as writer:
                        shutil.copyfileobj(reader, writer)
                    if destination.stat().st_size != entry["bytes"] or _sha256(destination) != entry["sha256"]:
                        raise RestoreSafetyError(f"backup integrity failure for {entry['path']}")
                if (staged / "memory" / "notes").exists():
                    MemoryService(staged).rebuild_index()
                artifacts = ArtifactStore(staged)
                for manifest_path in (staged / "artifacts" / "manifests").glob("*.json") if (staged / "artifacts" / "manifests").exists() else []:
                    artifact = json.loads(manifest_path.read_text(encoding="utf-8"))
                    data = artifacts.get_bytes(artifact["artifactId"])
                    if data is None or hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                        raise RestoreSafetyError("restored artifact checksum verification failed")
                os.replace(staged, target)
        return {
            "schema": "a2a-superhub.restore-result.v1",
            "productVersion": manifest.get("productVersion"),
            "integrity": "verified",
            "fileCount": len(manifest["files"]),
            "derivedIndexes": "rebuilt-from-authoritative-state",
        }


class RetentionManager:
    """Recoverable trash with durable tombstones and delivery safety gates."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).resolve()
        self.db_path = self.state_dir / "operations" / "operations.sqlite"
        self.trash_root = self.state_dir / "trash"

    @contextmanager
    def _connect(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tombstones(
                    kind TEXT NOT NULL, object_id TEXT NOT NULL, state TEXT NOT NULL,
                    original_path TEXT NOT NULL, trash_path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, restored_at TEXT,
                    PRIMARY KEY(kind, object_id)
                );
                CREATE TABLE IF NOT EXISTS operation_receipts(
                    receipt_id TEXT PRIMARY KEY, operation TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, metadata_json TEXT NOT NULL
                );
                PRAGMA user_version=1;
                """
            )
        self._recover_transitions()

    def _recover_transitions(self) -> None:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tombstones WHERE state IN ('preparing', 'restoring')"
            ).fetchall()
            for row in rows:
                original = _within(self.state_dir, self.state_dir / PurePosixPath(row["original_path"]))
                trash = _within(self.state_dir, self.state_dir / PurePosixPath(row["trash_path"]))
                original_ok = original.is_file() and _sha256(original) == row["sha256"]
                trash_ok = trash.is_file() and _sha256(trash) == row["sha256"]
                if row["state"] == "preparing":
                    if original_ok and not trash.exists():
                        connection.execute(
                            "DELETE FROM tombstones WHERE kind=? AND object_id=?",
                            (row["kind"], row["object_id"]),
                        )
                    elif trash_ok and not original.exists():
                        connection.execute(
                            "UPDATE tombstones SET state='trashed' WHERE kind=? AND object_id=?",
                            (row["kind"], row["object_id"]),
                        )
                    else:
                        raise RetentionBlockedError("retention transition requires manual integrity review")
                elif trash_ok and not original.exists():
                    connection.execute(
                        "UPDATE tombstones SET state='trashed' WHERE kind=? AND object_id=?",
                        (row["kind"], row["object_id"]),
                    )
                elif original_ok and not trash.exists():
                    connection.execute(
                        "UPDATE tombstones SET state='restored', restored_at=? WHERE kind=? AND object_id=?",
                        (_now(), row["kind"], row["object_id"]),
                    )
                else:
                    raise RetentionBlockedError("retention restore requires manual integrity review")

    @staticmethod
    def _require_admin(principal: Principal) -> None:
        if not principal.has("hub.admin") and not principal.has("memory.admin"):
            raise RetentionBlockedError("retention requires administrative authority")

    def _active_tombstone(self, kind: str, object_id: str):
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM tombstones WHERE kind=? AND object_id=? AND state='trashed'",
                (kind, object_id),
            ).fetchone()

    def _tombstone(self, kind: str, object_id: str):
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM tombstones WHERE kind=? AND object_id=?",
                (kind, object_id),
            ).fetchone()

    def _prepare_tombstone(self, *, kind: str, object_id: str, original: Path, trash: Path, digest: str, metadata: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tombstones(kind,object_id,state,original_path,trash_path,sha256,metadata_json,created_at,restored_at)
                VALUES(?,?,'preparing',?,?,?,?,?,NULL)
                ON CONFLICT(kind,object_id) DO UPDATE SET state='preparing', original_path=excluded.original_path,
                    trash_path=excluded.trash_path, sha256=excluded.sha256, metadata_json=excluded.metadata_json,
                    created_at=excluded.created_at, restored_at=NULL
                """,
                (
                    kind, object_id,
                    original.relative_to(self.state_dir).as_posix(),
                    trash.relative_to(self.state_dir).as_posix(),
                    digest, json.dumps(metadata, sort_keys=True), _now(),
                ),
            )

    def _set_tombstone_state(self, kind: str, object_id: str, state: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE tombstones SET state=?, restored_at=? WHERE kind=? AND object_id=?",
                (state, _now() if state == "restored" else None, kind, object_id),
            )

    def _assert_deliveries_acknowledged(self, note_id: str) -> None:
        ops = self.state_dir / "memory" / "ops.sqlite"
        if not ops.exists():
            return
        connection = sqlite3.connect(ops)
        connection.row_factory = sqlite3.Row
        try:
            deliveries = connection.execute(
                "SELECT sequence,recipient FROM deliveries WHERE note_id=?", (note_id,)
            ).fetchall()
            for delivery in deliveries:
                cursors = connection.execute(
                    "SELECT acked_sequence FROM consumer_cursors WHERE principal=?", (delivery["recipient"],)
                ).fetchall()
                if not cursors or any(int(cursor[0]) < int(delivery["sequence"]) for cursor in cursors):
                    raise RetentionBlockedError("note has unacknowledged delivery state")
        finally:
            connection.close()

    def trash_note(self, note_id: str, principal: Principal, *, allow_private: bool = False) -> dict[str, Any]:
        with StateLease(self.state_dir, purpose="retention"):
            self._init()
            return self._trash_note(note_id, principal, allow_private=allow_private)

    def _trash_note(self, note_id: str, principal: Principal, *, allow_private: bool = False) -> dict[str, Any]:
        self._require_admin(principal)
        existing = self._active_tombstone("memory-note", note_id)
        if existing:
            return {"kind": "memory-note", "objectId": note_id, "state": "trashed", "idempotent": True}
        service = MemoryService(self.state_dir, enable_delivery=True)
        service.init()
        try:
            note = service._read_authoritative(note_id)
            original = service._authoritative_catalog[note_id]
        except KeyError as exc:
            raise RetentionBlockedError("memory note does not exist") from exc
        if note["visibility"] != "shared" and not allow_private:
            raise RetentionBlockedError("private or direct memory requires explicit retention authority")
        self._assert_deliveries_acknowledged(note_id)
        trash = self.trash_root / "memory" / note_id / "note.md"
        trash.parent.mkdir(parents=True, exist_ok=True)
        if trash.exists():
            raise RetentionBlockedError("trash destination already exists")
        digest = _sha256(original)
        self._prepare_tombstone(kind="memory-note", object_id=note_id, original=original, trash=trash, digest=digest, metadata={"visibility": note["visibility"]})
        shutil.move(str(original), str(trash))
        self._set_tombstone_state("memory-note", note_id, "trashed")
        MemoryService(self.state_dir).sync_filesystem()
        return {"kind": "memory-note", "objectId": note_id, "state": "trashed", "recoverable": True}

    def trash_artifact(self, artifact_id: str, principal: Principal, *, allow_private: bool = False) -> dict[str, Any]:
        with StateLease(self.state_dir, purpose="retention"):
            self._init()
            return self._trash_artifact(artifact_id, principal, allow_private=allow_private)

    def _trash_artifact(self, artifact_id: str, principal: Principal, *, allow_private: bool = False) -> dict[str, Any]:
        self._require_admin(principal)
        existing = self._active_tombstone("artifact", artifact_id)
        if existing:
            return {"kind": "artifact", "objectId": artifact_id, "state": "trashed", "idempotent": True}
        store = ArtifactStore(self.state_dir)
        manifest = store.get_manifest(artifact_id)
        if manifest is None:
            raise RetentionBlockedError("artifact does not exist")
        if manifest.get("visibility", "private") != "shared" and not allow_private:
            raise RetentionBlockedError("private or direct artifact requires explicit retention authority")
        memory = MemoryService(self.state_dir)
        valid, _ = memory._scan_notes()
        for note, _ in valid.values():
            relations = note.get("relations") or []
            targets = {relation.get("target") for relation in relations if isinstance(relation, dict)}
            if artifact_id in set(note.get("artifacts") or []) | targets:
                raise RetentionBlockedError("artifact is still referenced by authoritative memory")
        original = store.manifests / f"{artifact_id}.json"
        trash = self.trash_root / "artifacts" / artifact_id / "manifest.json"
        trash.parent.mkdir(parents=True, exist_ok=True)
        if trash.exists():
            raise RetentionBlockedError("trash destination already exists")
        digest = _sha256(original)
        self._prepare_tombstone(kind="artifact", object_id=artifact_id, original=original, trash=trash, digest=digest, metadata={"visibility": manifest.get("visibility", "private"), "blobRetained": True})
        shutil.move(str(original), str(trash))
        self._set_tombstone_state("artifact", artifact_id, "trashed")
        return {"kind": "artifact", "objectId": artifact_id, "state": "trashed", "recoverable": True, "blobRetained": True}

    def restore(self, kind: str, object_id: str, principal: Principal) -> dict[str, Any]:
        with StateLease(self.state_dir, purpose="retention restore"):
            self._require_admin(principal)
            self._init()
            recovered = self._tombstone(kind, object_id)
            if recovered is not None and recovered["state"] == "restored":
                return {"kind": kind, "objectId": object_id, "state": "restored", "integrity": "verified", "idempotent": True}
            return self._restore(kind, object_id, principal)

    def _restore(self, kind: str, object_id: str, principal: Principal) -> dict[str, Any]:
        self._require_admin(principal)
        if kind not in {"memory-note", "artifact"}:
            raise RetentionBlockedError("unsupported retention object kind")
        row = self._active_tombstone(kind, object_id)
        if row is None:
            raise RetentionBlockedError("active tombstone does not exist")
        original = _within(self.state_dir, self.state_dir / PurePosixPath(row["original_path"]))
        trash = _within(self.state_dir, self.state_dir / PurePosixPath(row["trash_path"]))
        if not trash.is_file() or _sha256(trash) != row["sha256"]:
            raise RetentionBlockedError("trash integrity verification failed")
        if original.exists():
            raise RetentionBlockedError("restore destination already exists")
        original.parent.mkdir(parents=True, exist_ok=True)
        self._set_tombstone_state(kind, object_id, "restoring")
        shutil.move(str(trash), str(original))
        self._set_tombstone_state(kind, object_id, "restored")
        if kind == "memory-note":
            MemoryService(self.state_dir).sync_filesystem()
        return {"kind": kind, "objectId": object_id, "state": "restored", "integrity": "verified"}

    def list_tombstones(self) -> list[dict[str, Any]]:
        if not self.db_path.is_file():
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT kind,object_id,state,created_at,restored_at,metadata_json FROM tombstones ORDER BY created_at,kind,object_id"
            ).fetchall()
        return [{
            "kind": row["kind"], "objectId": row["object_id"], "state": row["state"],
            "createdAt": row["created_at"], "restoredAt": row["restored_at"],
            "metadata": json.loads(row["metadata_json"]),
        } for row in rows]


class SearchMigrationManager:
    """Rebuild-and-compare local and server derived search before activation."""

    def __init__(self, state_dir: str | Path, *, provider_factory: Callable[..., Any] | None = None):
        self.state_dir = Path(state_dir).resolve()
        if provider_factory is None:
            from .retrieval import QdrantRetrievalProvider
            provider_factory = QdrantRetrievalProvider
        self.provider_factory = provider_factory
        self.config_path = self.state_dir / "retrieval" / "provider.json"
        self.receipt_path = self.state_dir / "retrieval" / "migration.json"

    def _notes(self) -> list[dict[str, Any]]:
        service = MemoryService(self.state_dir)
        valid, collided = service._scan_notes()
        return [note for note, _ in valid.values() if note["id"].casefold() not in collided]

    def drill(
        self,
        server_url: str,
        *,
        queries: list[dict[str, Any]],
        cache_dir: str | Path | None = None,
        parity_min: float = 1.0,
        activate: bool = False,
    ) -> dict[str, Any]:
        with StateLease(self.state_dir, purpose="search migration"):
            return self._drill(
                server_url,
                queries=queries,
                cache_dir=cache_dir,
                parity_min=parity_min,
                activate=activate,
            )

    def _drill(
        self,
        server_url: str,
        *,
        queries: list[dict[str, Any]],
        cache_dir: str | Path | None = None,
        parity_min: float = 1.0,
        activate: bool = False,
    ) -> dict[str, Any]:
        parsed_url = urlsplit(server_url)
        if (
            parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise MigrationError("server URL must be explicit HTTP(S)")
        if not queries:
            raise MigrationError("migration drill requires at least one parity query")
        if not 0 <= parity_min <= 1:
            raise MigrationError("parity threshold must be between zero and one")
        notes = self._notes()
        if not notes:
            raise MigrationError("migration drill requires authoritative notes")
        note_map = {note["id"]: note for note in notes}
        authorization = MemoryService(self.state_dir, artifact_store=ArtifactStore(self.state_dir))
        local = self.provider_factory(self.state_dir, mode="local", cache_dir=cache_dir)
        server = self.provider_factory(self.state_dir, mode="server", url=server_url, cache_dir=cache_dir)
        local_build = local.rebuild(notes)
        server_build = server.rebuild(notes)
        matches = total = 0
        cases = []
        for query in queries:
            subject = str(query.get("principal") or "").strip()
            if not subject:
                raise MigrationError("each parity query requires a principal")
            principal = Principal(subject, "agent", "tok_migration", frozenset({"memory.read"}))
            kwargs = {"load_note": note_map.__getitem__, "can_read": authorization._can_read, "limit": int(query.get("limit", 10))}
            local_ids = [note["id"] for note in local.search(str(query["text"]), principal, **kwargs)]
            server_ids = [note["id"] for note in server.search(str(query["text"]), principal, **kwargs)]
            width = max(len(local_ids), len(server_ids), 1)
            case_matches = sum(
                1 for index in range(width)
                if index < len(local_ids) and index < len(server_ids) and local_ids[index] == server_ids[index]
            )
            matches += case_matches
            total += width
            cases.append({"queryIndex": len(cases), "matches": case_matches, "compared": width})
        parity = matches / total
        if parity < parity_min:
            raise MigrationError(f"local/server query parity {parity:.3f} is below {parity_min:.3f}")
        previous = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {"mode": "local"}
        result = {
            "schema": "a2a-superhub.search-migration.v1",
            "createdAt": _now(),
            "notes": len(notes),
            "queries": len(queries),
            "queryParity": parity,
            "parityThreshold": parity_min,
            "local": {"mode": "local", "build": local_build, "capabilities": local.capabilities()},
            "server": {"mode": "server", "build": server_build, "capabilities": server.capabilities()},
            "cases": cases,
            "activated": bool(activate),
            "previous": previous,
        }
        _atomic_json(self.receipt_path, result)
        if activate:
            _atomic_json(self.config_path, {"schema": SEARCH_PROVIDER_SCHEMA, "mode": "server", "url": server_url, "previous": previous})
        return result

    def rollback(self) -> dict[str, Any]:
        with StateLease(self.state_dir, purpose="search migration rollback"):
            return self._rollback()

    def _rollback(self) -> dict[str, Any]:
        current = json.loads(self.config_path.read_text(encoding="utf-8")) if self.config_path.exists() else {}
        previous = current.get("previous") if isinstance(current.get("previous"), dict) else {"mode": "local"}
        if previous.get("mode") not in {"local", "server", "keyword"}:
            previous = {"mode": "local"}
        restored = {
            "schema": SEARCH_PROVIDER_SCHEMA,
            "mode": previous.get("mode", "local"),
        }
        if restored["mode"] == "server" and isinstance(previous.get("url"), str):
            restored["url"] = previous["url"]
        _atomic_json(self.config_path, restored)
        return {"schema": "a2a-superhub.search-rollback.v1", **restored, "rolledBackAt": _now()}


def _db_count(path: Path, table: str, where: str = "") -> int:
    if not path.is_file():
        return 0
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table} {where}").fetchone()[0])
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def _state_inventory(root: Path) -> dict[str, tuple[int, int]]:
    """Collect all payload-free file counts in one non-following state walk."""
    inventory = {
        "artifacts": [0, 0],
        "blobs": [0, 0],
        "notes": [0, 0],
        "uploads": [0, 0],
        "state": [0, 0],
    }
    if not root.exists():
        return {key: (0, 0) for key in inventory}
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                size = int(entry.stat(follow_symlinks=False).st_size)
                relative = Path(entry.path).relative_to(root)
            except (OSError, ValueError):
                # SQLite sidecars and atomic temporary files can disappear while
                # a payload-free diagnostic walk is in progress.
                continue
            parts = relative.parts
            inventory["state"][0] += 1
            inventory["state"][1] += size
            category = None
            if len(parts) >= 3 and parts[:2] == ("artifacts", "manifests") and relative.suffix.casefold() == ".json":
                category = "artifacts"
            elif len(parts) >= 3 and parts[:2] == ("artifacts", "blobs"):
                category = "blobs"
            elif len(parts) >= 3 and parts[:2] == ("memory", "notes") and relative.suffix.casefold() == ".md":
                category = "notes"
            elif len(parts) >= 3 and parts[:2] == ("artifacts", "uploads") and relative.suffix.casefold() == ".json":
                category = "uploads"
            if category:
                inventory[category][0] += 1
                inventory[category][1] += size
    return {key: (value[0], value[1]) for key, value in inventory.items()}


class OperationsDiagnostics:
    """Sanitized state counts and version/queue/resource observations."""

    def __init__(self, state_dir: str | Path, *, memory_service: MemoryService | None = None):
        self.state_dir = Path(state_dir).resolve()
        if memory_service is not None and memory_service.state_dir.resolve() != self.state_dir:
            raise ValueError("diagnostics memory service must use the same state directory")
        self.memory_service = memory_service

    def collect(self, principal: Principal) -> dict[str, Any]:
        if not principal.has("hub.admin"):
            raise OperationsError("hub.admin scope required for operational diagnostics")
        tasks_db = self.state_dir / "tasks" / "hub-tasks.sqlite"
        ops_db = self.state_dir / "memory" / "ops.sqlite"
        operations_db = self.state_dir / "operations" / "operations.sqlite"
        inventory = _state_inventory(self.state_dir)
        artifact_records, artifact_bytes = inventory["artifacts"]
        blob_records, blob_bytes = inventory["blobs"]
        note_records, note_bytes = inventory["notes"]
        upload_records, upload_bytes = inventory["uploads"]
        state_records, state_bytes = inventory["state"]
        retrieval_manifest = None
        active = self.state_dir / "retrieval" / "active.json"
        if active.is_file():
            try:
                value = json.loads(active.read_text(encoding="utf-8"))
                manifest = value.get("manifest") or {}
                retrieval_manifest = {
                    "collection": value.get("collection"),
                    "manifestFingerprint": manifest.get("fingerprint"),
                    "denseModel": manifest.get("dense_model"),
                    "denseRevision": manifest.get("dense_revision"),
                    "sparseModel": manifest.get("sparse_model"),
                }
            except (OSError, ValueError):
                retrieval_manifest = {"degraded": "invalid-active-manifest"}
        index = {"sourceRevision": 0, "indexedRevision": 0, "lagRecords": 0, "degraded": []}
        if (self.state_dir / "memory" / "notes").exists():
            try:
                memory = self.memory_service or MemoryService(self.state_dir)
                index = memory.index_status(include_lag_records=True)
            except Exception as exc:
                index = {"sourceRevision": 0, "indexedRevision": 0, "lagRecords": 0, "degraded": [type(exc).__name__]}
        return {
            "schema": DIAGNOSTICS_SCHEMA,
            "productVersion": __version__,
            "generatedAt": _now(),
            "payloadFree": True,
            "stores": {
                "tasks": {
                    "records": _db_count(tasks_db, "tasks"),
                    "events": _db_count(tasks_db, "events"),
                    "pendingTerminalOutbox": _db_count(tasks_db, "terminal_outbox", "WHERE acknowledged_at IS NULL"),
                },
                "artifacts": {"records": artifact_records, "blobRecords": blob_records, "bytes": artifact_bytes + blob_bytes, "resumableUploads": upload_records, "uploadBytes": upload_bytes},
                "memory": {
                    "records": note_records, "bytes": note_bytes,
                    "pendingJobs": _db_count(ops_db, "jobs", "WHERE state NOT IN ('done', 'completed')"),
                    "deliveries": _db_count(ops_db, "deliveries"),
                    "consumers": _db_count(ops_db, "consumer_cursors"),
                    "activeQuarantine": _db_count(ops_db, "quarantine", "WHERE state = 'active'"),
                    "index": index,
                },
                "retention": {
                    "trashed": _db_count(operations_db, "tombstones", "WHERE state = 'trashed'"),
                    "restored": _db_count(operations_db, "tombstones", "WHERE state = 'restored'"),
                },
                "retrieval": retrieval_manifest,
            },
            "resources": {"stateFiles": state_records, "stateBytes": state_bytes},
        }
