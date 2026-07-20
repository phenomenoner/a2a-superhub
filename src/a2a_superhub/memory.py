from __future__ import annotations

import hashlib
import base64
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
import weakref
from datetime import datetime
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from .auth import Principal
from .models import TERMINAL_STATES, json_dumps, utc_now

NOTE_SCHEMA = "a2a-superhub.memory.note.v1"
NOTE_ID = re.compile(r"^mem_[0-9a-f]{32}$")
PRINCIPAL_ID = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
RELATION_TYPES = {"about", "relates_to", "depends_on", "blocks", "produced_by", "updates", "disputes", "references"}
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_IDEMPOTENCY_LOCKS: weakref.WeakValueDictionary[tuple[str, str, str, str], threading.RLock] = weakref.WeakValueDictionary()
_IDEMPOTENCY_LOCKS_GUARD = threading.Lock()


def _idempotency_lock(state: Path, principal: str, operation: str, key: str) -> threading.RLock:
    compound = (str(state.resolve()).casefold(), principal, operation, key)
    with _IDEMPOTENCY_LOCKS_GUARD:
        return _IDEMPOTENCY_LOCKS.setdefault(compound, threading.RLock())


def _idempotency_lock_count() -> int:
    with _IDEMPOTENCY_LOCKS_GUARD:
        return len(_IDEMPOTENCY_LOCKS)


class MemoryError(ValueError):
    code = "invalid_request"


class AuthorizationError(MemoryError):
    code = "forbidden"


class ConflictError(MemoryError):
    code = "conflict"


class QuarantineError(MemoryError):
    code = "quarantined"


class RequestTooLargeError(MemoryError):
    code = "request_too_large"


class CursorError(MemoryError):
    code = "cursor_invalid"


def _yaml_module():
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - packaging contract covers this branch
        raise RuntimeError("memory support requires: pip install a2a-superhub[memory-core]") from exc
    return yaml


def canonical_body(body: str) -> str:
    if not isinstance(body, str):
        raise MemoryError("body must be a string")
    value = body.replace("\r\n", "\n").replace("\r", "\n")
    if len(value.encode("utf-8")) > 262_144:
        raise RequestTooLargeError("note body is too large")
    return value


def note_path(root: Path, note_id: str) -> Path:
    if not NOTE_ID.fullmatch(note_id):
        raise MemoryError("invalid note id")
    relative = Path("notes") / note_id[4:6] / f"{note_id}.md"
    target = (root / relative).resolve()
    if root.resolve() not in target.parents:
        raise MemoryError("note path escapes memory root")
    for part in relative.parts:
        normalized = unicodedata.normalize("NFC", part)
        stem = normalized.split(".", 1)[0].casefold()
        if normalized != part or stem in RESERVED_NAMES:
            raise MemoryError("unsafe note path")
    return target


def validate_existing_path(root: Path, path: Path) -> Path:
    target = path.resolve()
    notes_root = (root / "notes").resolve()
    if notes_root not in target.parents:
        raise QuarantineError("note path escapes notes root")
    relative = target.relative_to(notes_root)
    for part in relative.parts:
        normalized = unicodedata.normalize("NFC", part)
        stem = normalized.split(".", 1)[0].casefold()
        if normalized != part or stem in RESERVED_NAMES or part in {".", ".."}:
            raise QuarantineError("unsafe note path")
    return target


def path_collision_key(root: Path, path: Path) -> str:
    target = validate_existing_path(root, path)
    relative = target.relative_to((root / "notes").resolve())
    return "/".join(unicodedata.normalize("NFC", part).casefold() for part in relative.parts)


def _validate_note(note: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "schema", "id", "type", "title", "author", "visibility", "recordedAt", "occurredAt", "updatedAt",
        "source", "project", "participants", "about", "tags", "artifacts", "supersedes", "relations", "body",
    }
    extra = sorted(set(note) - allowed)
    if extra:
        raise MemoryError(f"unknown note fields: {', '.join(extra)}")
    required = {"schema", "id", "type", "title", "author", "visibility", "recordedAt", "source", "body"}
    missing = sorted(required - note.keys())
    if missing:
        raise MemoryError(f"missing note fields: {', '.join(missing)}")
    if note["schema"] != NOTE_SCHEMA or not isinstance(note["id"], str) or not NOTE_ID.fullmatch(note["id"]):
        raise MemoryError("invalid note schema or id")
    if note["type"] not in {"note", "decision", "handoff", "observation", "task-log", "profile"}:
        raise MemoryError("invalid note type")
    if not isinstance(note["title"], str) or not 1 <= len(note["title"]) <= 256:
        raise MemoryError("invalid title")
    if not isinstance(note["author"], str) or not PRINCIPAL_ID.fullmatch(note["author"]):
        raise MemoryError("invalid author")
    if not isinstance(note["visibility"], str):
        raise MemoryError("invalid visibility")
    visibility = note["visibility"]
    if visibility not in {"private", "shared"} and not (
        visibility.startswith("direct:") and PRINCIPAL_ID.fullmatch(visibility[7:])
    ):
        raise MemoryError("invalid visibility")
    for field in ("recordedAt", "occurredAt", "updatedAt"):
        if field in note:
            value = note[field]
            if not isinstance(value, str):
                raise MemoryError(f"invalid {field}")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
                raise MemoryError(f"invalid {field}")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise MemoryError(f"invalid {field}") from exc
            if parsed.tzinfo is None:
                raise MemoryError(f"invalid {field}")
    source = note["source"]
    if not isinstance(source, dict) or set(source) - {"kind", "taskId", "eventSeq", "artifactId", "relativePath"} or source.get("kind") not in {"api", "cli", "filesystem", "task-event", "federation"}:
        raise MemoryError("invalid source")
    for field in ("taskId", "relativePath"):
        if field in source and (not isinstance(source[field], str) or not 1 <= len(source[field]) <= (128 if field == "taskId" else 512)):
            raise MemoryError("invalid source")
    if "eventSeq" in source and (not isinstance(source["eventSeq"], int) or isinstance(source["eventSeq"], bool) or source["eventSeq"] < 1):
        raise MemoryError("invalid source")
    if "artifactId" in source and (not isinstance(source["artifactId"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", source["artifactId"])):
        raise MemoryError("invalid source")
    if "project" in note and (not isinstance(note["project"], str) or not 1 <= len(note["project"]) <= 128):
        raise MemoryError("invalid project")
    for field in ("participants", "about"):
        if field in note:
            values = note[field]
            if not isinstance(values, list) or len(values) > 32 or any(not isinstance(value, str) or not PRINCIPAL_ID.fullmatch(value) for value in values) or len(values) != len(set(values)):
                raise MemoryError(f"invalid {field}")
    if "tags" in note:
        tags = note["tags"]
        if not isinstance(tags, list) or len(tags) > 32 or any(not isinstance(tag, str) or not 1 <= len(tag) <= 64 for tag in tags) or len(tags) != len(set(tags)):
            raise MemoryError("invalid tags")
    if "artifacts" in note:
        artifacts = note["artifacts"]
        if not isinstance(artifacts, list) or len(artifacts) > 32 or any(not isinstance(item, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", item) for item in artifacts) or len(artifacts) != len(set(artifacts)):
            raise MemoryError("invalid artifacts")
    if "supersedes" in note and (not isinstance(note["supersedes"], str) or not NOTE_ID.fullmatch(note["supersedes"])):
        raise MemoryError("invalid supersedes")
    relations = note.get("relations", [])
    if not isinstance(relations, list) or len(relations) > 128:
        raise MemoryError("invalid relations")
    for relation in relations:
        relation_type = relation.get("type") if isinstance(relation, dict) else None
        if relation_type not in RELATION_TYPES and not re.fullmatch(r"x-[a-z0-9][a-z0-9.-]{0,62}", str(relation_type)):
            raise MemoryError("invalid relation")
        if set(relation) != {"type", "target"}:
            raise MemoryError("invalid relation")
        if not isinstance(relation.get("target"), str) or not 1 <= len(relation["target"]) <= 256:
            raise MemoryError("invalid relation target")
    note["body"] = canonical_body(note["body"])
    return note


def serialize_note(note: dict[str, Any]) -> bytes:
    yaml = _yaml_module()
    note = _validate_note(dict(note))
    body = note.pop("body")
    header = yaml.safe_dump(note, allow_unicode=True, sort_keys=True, default_flow_style=False).rstrip("\n")
    return f"---\n{header}\n---\n{body}".encode("utf-8")


def parse_note(data: bytes) -> dict[str, Any]:
    yaml = _yaml_module()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise QuarantineError("note is not valid UTF-8") from exc
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise QuarantineError("partial or missing YAML frontmatter")
    header, body = text[4:].split("\n---\n", 1)
    if header.count("&") + header.count("*") > 32:
        raise QuarantineError("excessive YAML aliases")
    try:
        metadata = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise QuarantineError("unsafe or invalid YAML frontmatter") from exc
    if not isinstance(metadata, dict):
        raise QuarantineError("frontmatter must be an object")
    metadata["body"] = body
    try:
        return _validate_note(metadata)
    except MemoryError as exc:
        raise QuarantineError(str(exc)) from exc


def _hit_failpoint(failpoint: str | Callable[[str], None] | None, stage: str) -> None:
    if callable(failpoint):
        failpoint(stage)
    elif failpoint == stage:
        raise RuntimeError(f"failpoint:{stage}")


def atomic_write(path: Path, data: bytes, *, failpoint: str | Callable[[str], None] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _hit_failpoint(failpoint, "before_replace")
        os.replace(temp, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        if temp.exists():
            temp.unlink()


@dataclass(frozen=True)
class CreateResult:
    note: dict[str, Any]
    inserted: bool
    revision: str
    trace_id: str = ""


class MemoryService:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        now: Callable[[], str] = utc_now,
        new_note_id: Callable[[], str] | None = None,
        enable_delivery: bool = False,
        enable_task_log: bool = False,
        enable_watcher_side_effects: bool = False,
        cursor_secret: bytes | None = None,
        task_log_intents: set[str] | frozenset[str] | None = None,
        hub_store: Any | None = None,
        search_provider: Any | None = None,
        artifact_store: Any | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "memory"
        self.ops_path = self.root / "ops.sqlite"
        self.index_path = self.root / "index.sqlite"
        self.now = now
        self.new_note_id = new_note_id or (lambda: f"mem_{uuid.uuid4().hex}")
        self.enable_delivery = enable_delivery
        self.enable_task_log = enable_task_log
        self.enable_watcher_side_effects = enable_watcher_side_effects
        self._configured_cursor_secret = cursor_secret
        self.task_log_intents = frozenset(task_log_intents or set())
        self.hub_store = hub_store
        self.search_provider = search_provider
        self.artifact_store = artifact_store
        self._initialized = False
        self._authoritative_catalog: dict[str, Path] = {}

    @contextmanager
    def _connect(self, path: Path) -> Iterator[sqlite3.Connection]:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        if self._initialized:
            try:
                conn = sqlite3.connect(self.ops_path)
                try:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)")}
                finally:
                    conn.close()
                if version == 3 and {"principal", "operation", "trace_id"} <= columns:
                    return
            except sqlite3.Error:
                pass
            self._initialized = False
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect(self.ops_path) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS idempotency(key TEXT NOT NULL, principal TEXT NOT NULL, operation TEXT NOT NULL, request_hash TEXT NOT NULL, note_id TEXT NOT NULL, trace_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(principal, operation, key));
                CREATE TABLE IF NOT EXISTS jobs(operation_id TEXT PRIMARY KEY, note_id TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS quarantine(path TEXT PRIMARY KEY, reason TEXT NOT NULL, observed_at TEXT NOT NULL, resolved_at TEXT, state TEXT NOT NULL DEFAULT 'active');
                CREATE TABLE IF NOT EXISTS deliveries(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    delivery_id TEXT NOT NULL UNIQUE,
                    note_id TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    trace_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(note_id, recipient, reason)
                );
                CREATE TABLE IF NOT EXISTS consumer_cursors(
                    principal TEXT NOT NULL,
                    consumer_id TEXT NOT NULL,
                    acked_sequence INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(principal, consumer_id)
                );
                CREATE TABLE IF NOT EXISTS issued_cursors(
                    cursor_hash TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    consumer_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    issued_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ops_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS receipts(
                    trace_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(trace_id, phase, operation_id)
                );
                INSERT OR IGNORE INTO migrations(version, applied_at) VALUES (1, 'durable-memory-foundation');
                INSERT OR IGNORE INTO migrations(version, applied_at) VALUES (2, 'offline-sharing-foundation');
                UPDATE jobs SET state = 'pending' WHERE state = 'running';
                PRAGMA user_version=3;
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)").fetchall()}
            if "principal" not in columns or "operation" not in columns:
                conn.executescript(
                    """
                    ALTER TABLE idempotency RENAME TO idempotency_legacy;
                    CREATE TABLE idempotency(key TEXT NOT NULL, principal TEXT NOT NULL, operation TEXT NOT NULL, request_hash TEXT NOT NULL, note_id TEXT NOT NULL, trace_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, PRIMARY KEY(principal, operation, key));
                    INSERT OR IGNORE INTO idempotency(key, principal, operation, request_hash, note_id, trace_id, created_at)
                    SELECT key, 'local.operator', 'memory.note.create.api', request_hash, note_id, '', created_at FROM idempotency_legacy;
                    DROP TABLE idempotency_legacy;
                    """
                )
            idempotency_columns = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)").fetchall()}
            if "trace_id" not in idempotency_columns:
                conn.execute("ALTER TABLE idempotency ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
            quarantine_columns = {row[1] for row in conn.execute("PRAGMA table_info(quarantine)").fetchall()}
            if "resolved_at" not in quarantine_columns:
                conn.execute("ALTER TABLE quarantine ADD COLUMN resolved_at TEXT")
            if "state" not in quarantine_columns:
                conn.execute("ALTER TABLE quarantine ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")
            delivery_columns = {row[1] for row in conn.execute("PRAGMA table_info(deliveries)").fetchall()}
            if "trace_id" not in delivery_columns:
                conn.execute("ALTER TABLE deliveries ADD COLUMN trace_id TEXT NOT NULL DEFAULT ''")
        self._init_index()
        valid, collided_ids = self._scan_notes()
        self._recover_jobs_from_scan(valid, collided_ids)
        if self.enable_delivery:
            with self._connect(self.ops_path) as conn:
                backfilled = conn.execute(
                    "SELECT value FROM ops_metadata WHERE key='delivery_backfill_version'"
                ).fetchone()
            corpus_revision = self._corpus_revision(valid)
            if not backfilled or backfilled["value"] != corpus_revision:
                self._generate_all_deliveries(valid)
                with self._connect(self.ops_path) as conn:
                    conn.execute(
                        "INSERT INTO ops_metadata(key, value) VALUES ('delivery_backfill_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (corpus_revision,),
                    )
        self._initialized = True

    def _init_index(self, index_path: Path | None = None) -> None:
        target = index_path or self.index_path
        with self._connect(target) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=DELETE;
                CREATE TABLE IF NOT EXISTS notes(note_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE, author TEXT NOT NULL, visibility TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, recorded_at TEXT NOT NULL, content_hash TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS relations(note_id TEXT NOT NULL, relation_type TEXT NOT NULL, target TEXT NOT NULL, PRIMARY KEY(note_id, relation_type, target));
                CREATE TABLE IF NOT EXISTS manifest(note_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, content_hash TEXT NOT NULL, revision INTEGER NOT NULL);
                """
            )
            try:
                conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(note_id UNINDEXED, title, body)")
            except sqlite3.OperationalError:
                pass
            columns = {row[1] for row in conn.execute("PRAGMA table_info(notes)").fetchall()}
            additions = {
                "note_type": "TEXT NOT NULL DEFAULT 'note'",
                "project": "TEXT",
                "participants_json": "TEXT NOT NULL DEFAULT '[]'",
                "about_json": "TEXT NOT NULL DEFAULT '[]'",
                "supersedes": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE notes ADD COLUMN {name} {definition}")

    def _can_read(self, principal: Principal, note: dict[str, Any]) -> bool:
        if not principal.has("memory.read"):
            return False
        if not (principal.has("memory.admin") or note["author"] == principal.subject):
            visibility = note["visibility"]
            if visibility != "shared" and visibility != f"direct:{principal.subject}":
                return False
        derived_sources = [
            item.get("target", "")[len("artifact:"):]
            for item in note.get("relations") or []
            if item.get("type") == "x-derived-from" and item.get("target", "").startswith("artifact:")
        ]
        if derived_sources:
            if self.artifact_store is None:
                return False
            for artifact_id in derived_sources:
                try:
                    manifest = self.artifact_store.get_manifest(artifact_id)
                    if not manifest or not self.artifact_store.can_read(manifest, principal):
                        return False
                except Exception:
                    return False
        return True

    @staticmethod
    def _authorize_create(principal: Principal, visibility: str) -> None:
        if not principal.has("memory.write"):
            raise AuthorizationError("memory.write scope required")
        if visibility != "private" and not (principal.has("memory.share") or principal.has("memory.admin")):
            raise AuthorizationError("memory.share scope required")

    def create_note(
        self,
        request: dict[str, Any],
        principal: Principal,
        *,
        idempotency_key: str | None = None,
        failpoint: str | Callable[[str], None] | None = None,
        source_kind: str = "api",
        trace_id: str | None = None,
    ) -> CreateResult:
        if source_kind not in {"api", "cli"}:
            raise MemoryError("unsupported create operation")
        if idempotency_key:
            operation = f"memory.note.create.{source_kind}"
            with _idempotency_lock(self.state_dir, principal.subject, operation, idempotency_key):
                return self._create_note_owned(
                    request, principal, idempotency_key=idempotency_key, failpoint=failpoint,
                    source_kind=source_kind, trace_id=trace_id,
                )
        return self._create_note_owned(
            request, principal, idempotency_key=idempotency_key, failpoint=failpoint,
            source_kind=source_kind, trace_id=trace_id,
        )

    def _create_note_owned(
        self,
        request: dict[str, Any],
        principal: Principal,
        *,
        idempotency_key: str | None,
        failpoint: str | Callable[[str], None] | None,
        source_kind: str,
        trace_id: str | None,
    ) -> CreateResult:
        self.init()
        operation = f"memory.note.create.{source_kind}"
        supplied_trace_id = trace_id
        request = dict(request)
        missing_request = {"type", "title", "visibility", "body"} - request.keys()
        if missing_request:
            raise MemoryError(f"missing create fields: {', '.join(sorted(missing_request))}")
        request["body"] = canonical_body(request["body"])
        if idempotency_key is not None and not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", idempotency_key):
            raise MemoryError("invalid idempotency key")
        visibility = str(request.get("visibility") or "private")
        self._authorize_create(principal, visibility)
        forbidden = {"id", "author", "recordedAt", "schema", "source"} & request.keys()
        if forbidden:
            raise MemoryError(f"server-derived fields are forbidden: {', '.join(sorted(forbidden))}")
        request_hash = hashlib.sha256(json_dumps(request).encode("utf-8")).hexdigest()
        reserved_note_id: str | None = None
        if idempotency_key:
            with self._connect(self.ops_path) as conn:
                row = conn.execute(
                    "SELECT * FROM idempotency WHERE principal = ? AND operation = ? AND key = ?",
                    (principal.subject, operation, idempotency_key),
                ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise ConflictError("idempotency key reused with different request")
                reserved_note_id = row["note_id"]
                trace_id = row["trace_id"] or self._idempotent_trace_id(principal.subject, operation, idempotency_key)
                if not row["trace_id"]:
                    with self._connect(self.ops_path) as conn:
                        conn.execute(
                            "UPDATE idempotency SET trace_id=? WHERE principal=? AND operation=? AND key=?",
                            (trace_id, principal.subject, operation, idempotency_key),
                        )
                try:
                    note = self._read_authoritative(reserved_note_id)
                except KeyError:
                    pass
                else:
                    self._reconcile_create_operation(note, principal, trace_id)
                    return CreateResult(note, False, self._revision(note), trace_id)
            else:
                trace_id = supplied_trace_id or self._idempotent_trace_id(principal.subject, operation, idempotency_key)
        else:
            trace_id = supplied_trace_id or f"trace_{uuid.uuid4().hex}"
        note = dict(request)
        note.update(
            {
                "schema": NOTE_SCHEMA,
                "id": reserved_note_id or self._allocate_note_id(),
                "author": principal.subject,
                "recordedAt": self.now(),
                "source": {"kind": source_kind},
                "type": request.get("type") or "note",
            }
        )
        note = _validate_note(note)
        if note.get("supersedes"):
            previous = self._read_authoritative(note["supersedes"])
            if previous["author"] != principal.subject and not principal.has("memory.admin"):
                raise AuthorizationError("only the same author or memory.admin may supersede")
        if idempotency_key and reserved_note_id is None:
            lost_cross_process_reservation = False
            with self._connect(self.ops_path) as conn:
                try:
                    conn.execute(
                        "INSERT INTO idempotency(key, principal, operation, request_hash, note_id, trace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (idempotency_key, principal.subject, operation, request_hash, note["id"], trace_id, self.now()),
                    )
                except sqlite3.IntegrityError:
                    winner = conn.execute(
                        "SELECT * FROM idempotency WHERE principal=? AND operation=? AND key=?",
                        (principal.subject, operation, idempotency_key),
                    ).fetchone()
                    if not winner or winner["request_hash"] != request_hash:
                        raise ConflictError("idempotency key reused with different request")
                    reserved_note_id = winner["note_id"]
                    trace_id = winner["trace_id"] or self._idempotent_trace_id(principal.subject, operation, idempotency_key)
                    lost_cross_process_reservation = True
            if lost_cross_process_reservation:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    try:
                        winner_note = self._read_authoritative(reserved_note_id)
                    except KeyError:
                        time.sleep(0.01)
                        continue
                    self._reconcile_create_operation(winner_note, principal, trace_id)
                    return CreateResult(winner_note, False, self._revision(winner_note), trace_id)
                raise ConflictError("idempotency reservation is still in progress; retry safely")
        path = note_path(self.root, note["id"])
        atomic_write(path, serialize_note(note), failpoint=failpoint)
        self._authoritative_catalog[note["id"].casefold()] = path.resolve()
        self.record_receipt(trace_id, "write", f"write:{note['id']}", principal.subject, "committed", {"noteId": note["id"]})
        _hit_failpoint(failpoint, "after_replace_before_job")
        operation_id = self._job_operation(note, path)
        with self._connect(self.ops_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(operation_id, note_id, state, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
                (operation_id, note["id"], self.now(), self.now()),
            )
        _hit_failpoint(failpoint, "after_job_before_response")
        self.process_jobs()
        self.record_receipt(trace_id, "index", operation_id, principal.subject, "indexed", {"noteId": note["id"], "revision": self.source_revision(note["id"])})
        if self.enable_delivery:
            self.generate_deliveries(note["id"], trace_id=trace_id)
        _hit_failpoint(failpoint, "after_delivery_before_response")
        return CreateResult(note, True, self._revision(note), trace_id)

    @staticmethod
    def _idempotent_trace_id(principal: str, operation: str, key: str) -> str:
        digest = hashlib.sha256(f"{principal}\0{operation}\0{key}".encode("utf-8")).hexdigest()
        return f"trace_{digest[:32]}"

    def _reconcile_create_operation(self, note: dict[str, Any], principal: Principal, trace_id: str) -> None:
        path = note_path(self.root, note["id"])
        operation_id = self._job_operation(note, path)
        self.record_receipt(trace_id, "write", f"write:{note['id']}", principal.subject, "committed", {"noteId": note["id"]})
        with self._connect(self.ops_path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(operation_id, note_id, state, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?)",
                (operation_id, note["id"], self.now(), self.now()),
            )
        self.process_jobs()
        self.record_receipt(trace_id, "index", operation_id, principal.subject, "indexed", {"noteId": note["id"], "revision": self.source_revision(note["id"])})
        if self.enable_delivery:
            self.generate_deliveries(note["id"], trace_id=trace_id)

    @staticmethod
    def _delivery_id(note_id: str, recipient: str, reason: str) -> str:
        digest = hashlib.sha256(f"{note_id}\0{recipient}\0{reason}".encode("utf-8")).hexdigest()
        return f"del_{digest}"

    def _delivery_targets(self, note: dict[str, Any]) -> set[tuple[str, str]]:
        targets = {(str(subject), "about") for subject in note.get("about") or []}
        visibility = note["visibility"]
        if visibility.startswith("direct:"):
            recipient = visibility[7:]
            targets.add((recipient, "direct"))
            if note["type"] == "handoff":
                targets.add((recipient, "handoff"))
        return {(recipient, reason) for recipient, reason in targets if recipient != note["author"]}

    @staticmethod
    def _corpus_revision(valid: dict[str, tuple[dict[str, Any], Path]]) -> str:
        digest = hashlib.sha256()
        for key, (note, path) in sorted(valid.items()):
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(path).encode("utf-8"))
            digest.update(b"\0")
            digest.update(MemoryService._revision(note).encode("ascii"))
            digest.update(b"\0")
        return "sha256:" + digest.hexdigest()

    def _generate_all_deliveries(self, valid: dict[str, tuple[dict[str, Any], Path]] | None = None) -> int:
        inserted = 0
        if valid is None:
            valid, _ = self._scan_notes()
        with self._connect(self.ops_path) as conn:
            for note, _ in valid.values():
                inserted += self._generate_deliveries_for_note(note, conn=conn)
        return inserted

    def generate_deliveries(self, note_id: str, *, trace_id: str | None = None) -> int:
        if not self.enable_delivery:
            return 0
        note = self._read_authoritative(note_id)
        return self._generate_deliveries_for_note(note, trace_id=trace_id)

    def _generate_deliveries_for_note(
        self,
        note: dict[str, Any],
        *,
        trace_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        inserted = 0
        supplied_trace = trace_id is not None
        trace_id = trace_id or f"trace_delivery_{hashlib.sha256(note['id'].encode()).hexdigest()[:16]}"
        def apply(target: sqlite3.Connection) -> None:
            nonlocal inserted
            for recipient, reason in sorted(self._delivery_targets(note)):
                delivery_id = self._delivery_id(note["id"], recipient, reason)
                result = target.execute(
                    """
                    INSERT OR IGNORE INTO deliveries(delivery_id, note_id, recipient, reason, trace_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (delivery_id, note["id"], recipient, reason, trace_id, self.now()),
                )
                inserted += result.rowcount
                if result.rowcount == 0 and supplied_trace:
                    target.execute(
                        "UPDATE deliveries SET trace_id=? WHERE delivery_id=? AND trace_id LIKE 'trace_delivery_%'",
                        (trace_id, delivery_id),
                    )
                self._insert_receipt_conn(
                    target, trace_id, "delivery", delivery_id, note["author"], "queued",
                    {"noteId": note["id"], "recipient": recipient, "reason": reason},
                )
        if conn is not None:
            apply(conn)
        else:
            with self._connect(self.ops_path) as owned_conn:
                apply(owned_conn)
        return inserted

    def _insert_receipt_conn(
        self,
        conn: sqlite3.Connection,
        trace_id: str,
        phase: str,
        operation_id: str,
        subject: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        allowed = {"noteId", "recipient", "reason", "consumerId", "sequence", "revision", "count", "terminalState"}
        sanitized = {key: value for key, value in (metadata or {}).items() if key in allowed and isinstance(value, (str, int, bool))}
        conn.execute(
            "INSERT OR IGNORE INTO receipts VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trace_id, phase, operation_id, subject, status, self.now(), json_dumps(sanitized)),
        )

    def record_receipt(
        self,
        trace_id: str,
        phase: str,
        operation_id: str,
        subject: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect(self.ops_path) as conn:
            self._insert_receipt_conn(conn, trace_id, phase, operation_id, subject, status, metadata)

    def list_receipts(self, *, trace_id: str | None = None) -> list[dict[str, Any]]:
        self.init()
        with self._connect(self.ops_path) as conn:
            rows = conn.execute(
                "SELECT * FROM receipts WHERE (? IS NULL OR trace_id=?) ORDER BY created_at, phase, operation_id",
                (trace_id, trace_id),
            ).fetchall()
        return [
            {
                "traceId": row["trace_id"], "phase": row["phase"], "operationId": row["operation_id"],
                "subject": row["subject"], "status": row["status"], "createdAt": row["created_at"],
                "metadata": json.loads(row["metadata_json"]),
            }
            for row in rows
        ]

    def list_deliveries(self) -> list[dict[str, Any]]:
        self.init()
        with self._connect(self.ops_path) as conn:
            rows = conn.execute("SELECT * FROM deliveries ORDER BY sequence").fetchall()
        return [
            {
                "sequence": row["sequence"],
                "deliveryId": row["delivery_id"],
                "noteId": row["note_id"],
                "recipient": row["recipient"],
                "reason": row["reason"],
                "createdAt": row["created_at"],
            }
            for row in rows
        ]

    def _cursor_secret(self) -> bytes:
        if self._configured_cursor_secret is not None:
            return self._configured_cursor_secret
        with self._connect(self.ops_path) as conn:
            row = conn.execute("SELECT value FROM ops_metadata WHERE key='cursor_secret'").fetchone()
            if not row:
                value = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
                conn.execute("INSERT OR IGNORE INTO ops_metadata(key, value) VALUES ('cursor_secret', ?)", (value,))
                row = conn.execute("SELECT value FROM ops_metadata WHERE key='cursor_secret'").fetchone()
        return base64.urlsafe_b64decode(row["value"])

    def _encode_cursor(self, principal: str, consumer_id: str, sequence: int, *, nonce: str | None = None) -> str:
        payload = json_dumps({"v": 1, "p": principal, "c": consumer_id, "s": sequence, "n": nonce or uuid.uuid4().hex}).encode("utf-8")
        signature = hmac.new(self._cursor_secret(), payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode("ascii").rstrip("=")

    def _decode_cursor(self, token: str, principal: str, consumer_id: str) -> tuple[int, str]:
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
            payload, signature = raw[:-32], raw[-32:]
            expected = hmac.new(self._cursor_secret(), payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise CursorError("invalid cursor")
            value = json.loads(payload.decode("utf-8"))
            if value != {"v": 1, "p": principal, "c": consumer_id, "s": value.get("s"), "n": value.get("n")}:
                raise CursorError("invalid cursor binding")
            sequence = value["s"]
            nonce = value["n"]
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0 or not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
                raise CursorError("invalid cursor sequence")
            return sequence, nonce
        except CursorError:
            raise
        except Exception as exc:
            raise CursorError("invalid cursor") from exc

    def _issue_cursor(self, principal: str, consumer_id: str, sequence: int) -> str:
        token = self._encode_cursor(principal, consumer_id, sequence)
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        with self._connect(self.ops_path) as conn:
            conn.execute(
                "INSERT INTO issued_cursors VALUES (?, ?, ?, ?, ?)",
                (digest, principal, consumer_id, sequence, self.now()),
            )
        return token

    def fetch_inbox(
        self,
        principal: Principal,
        consumer_id: str,
        *,
        limit: int = 100,
        failpoint: str | Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self.init()
        if not principal.has("memory.read") or not PRINCIPAL_ID.fullmatch(consumer_id):
            raise AuthorizationError("memory.read scope and valid consumer are required")
        with self._connect(self.ops_path) as conn:
            cursor_row = conn.execute(
                "SELECT acked_sequence FROM consumer_cursors WHERE principal=? AND consumer_id=?",
                (principal.subject, consumer_id),
            ).fetchone()
            acknowledged = int(cursor_row[0]) if cursor_row else 0
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE recipient=? AND sequence>? ORDER BY sequence LIMIT ?",
                (principal.subject, acknowledged, max(1, min(limit, 100))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        scanned = acknowledged
        for row in rows:
            scanned = max(scanned, int(row["sequence"]))
            try:
                note = self.read_note(row["note_id"], principal)
            except (KeyError, QuarantineError):
                continue
            items.append(
                {
                    "deliveryId": row["delivery_id"],
                    "reason": row["reason"],
                    "note": note,
                    "provenance": {"noteId": note["id"], "author": note["author"], "recordedAt": note["recordedAt"]},
                }
            )
        result = {"consumerId": consumer_id, "items": items, "cursor": self._issue_cursor(principal.subject, consumer_id, scanned)}
        _hit_failpoint(failpoint, "after_fetch_before_response")
        return result

    def acknowledge_inbox(
        self,
        principal: Principal,
        consumer_id: str,
        cursor: str,
        *,
        failpoint: str | Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self.init()
        if not principal.has("memory.read") or not PRINCIPAL_ID.fullmatch(consumer_id):
            raise AuthorizationError("memory.read scope and valid consumer are required")
        sequence, _ = self._decode_cursor(cursor, principal.subject, consumer_id)
        cursor_hash = hashlib.sha256(cursor.encode("ascii")).hexdigest()
        with self._connect(self.ops_path) as conn:
            issued = conn.execute(
                "SELECT 1 FROM issued_cursors WHERE cursor_hash=? AND principal=? AND consumer_id=? AND sequence=?",
                (cursor_hash, principal.subject, consumer_id, sequence),
            ).fetchone()
            if not issued:
                raise CursorError("cursor was not issued")
            row = conn.execute(
                "SELECT acked_sequence FROM consumer_cursors WHERE principal=? AND consumer_id=?",
                (principal.subject, consumer_id),
            ).fetchone()
            current = int(row[0]) if row else 0
            advanced = sequence > current
            new_value = max(current, sequence)
            if advanced:
                receipt_rows = conn.execute(
                    "SELECT * FROM deliveries WHERE recipient=? AND sequence>? AND sequence<=? ORDER BY sequence",
                    (principal.subject, current, new_value),
                ).fetchall()
                for delivery in receipt_rows:
                    delivery_trace_id = delivery["trace_id"] or f"trace_delivery_{hashlib.sha256(delivery['note_id'].encode()).hexdigest()[:16]}"
                    self._insert_receipt_conn(
                        conn, delivery_trace_id, "ack", delivery["delivery_id"], principal.subject, "acknowledged",
                        {"consumerId": consumer_id, "sequence": delivery["sequence"]},
                    )
            conn.execute(
                """
                INSERT INTO consumer_cursors(principal, consumer_id, acked_sequence, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(principal, consumer_id) DO UPDATE SET
                    acked_sequence=MAX(consumer_cursors.acked_sequence, excluded.acked_sequence),
                    updated_at=excluded.updated_at
                """,
                (principal.subject, consumer_id, new_value, self.now()),
            )
        _hit_failpoint(failpoint, "after_ack_commit_before_response")
        return {"consumerId": consumer_id, "acked": advanced, "cursor": self._issue_cursor(principal.subject, consumer_id, new_value)}

    def wakeup(self, principal: Principal, consumer_id: str, *, budget_bytes: int = 65_536) -> dict[str, Any]:
        if budget_bytes < 256 or budget_bytes > 65_536:
            raise MemoryError("wakeup budget must be between 256 and 65536 UTF-8 bytes")
        fetched = self.fetch_inbox(principal, consumer_id)
        history = self.timeline(principal, include_superseded=False, limit=100)
        profiles = [
            note for note in history
            if note["type"] == "profile" and (note["author"] == principal.subject or principal.subject in (note.get("about") or []))
        ][:1]
        profile_items = [self._wakeup_note_item(note, "profile") for note in profiles]
        seen_note_ids = {note["id"] for note in profiles}
        inbox_items: list[dict[str, Any]] = []
        for item in fetched["items"]:
            seen_note_ids.add(item["note"]["id"])
            packed = self._wakeup_note_item(item["note"], "inbox")
            packed.update({"deliveryId": item["deliveryId"], "reason": item["reason"]})
            inbox_items.append(packed)
        participated = [
            note for note in history
            if note["id"] not in seen_note_ids
            and note["type"] != "profile"
            and (note["author"] == principal.subject or principal.subject in (note.get("participants") or []))
        ][:10]
        recent_items = [self._wakeup_note_item(note, "recent") for note in participated]
        active_task_items = self._active_task_wakeup_items(principal)
        section_candidates = [
            ("profile", profile_items),
            ("inbox", inbox_items),
            ("recent", recent_items),
            ("activeTasks", active_task_items),
        ]
        envelope: dict[str, Any] = {
            "role": "data",
            "trust": "untrusted-memory",
            "consumerId": consumer_id,
            "cursor": fetched["cursor"],
            "sections": [{"kind": kind, "items": []} for kind, _ in section_candidates],
            "items": [],
            "truncated": False,
        }
        for section_index, (_, candidates) in enumerate(section_candidates):
            for candidate in candidates:
                proposed = json.loads(json_dumps(envelope))
                proposed["sections"][section_index]["items"].append(candidate)
                if section_index == 1:
                    proposed["items"].append(candidate)
                if len(json_dumps(proposed).encode("utf-8")) > budget_bytes:
                    envelope["truncated"] = True
                    break
                envelope = proposed
            if envelope["truncated"]:
                break
        if len(json_dumps(envelope).encode("utf-8")) > budget_bytes:
            envelope = {
                "role": "data", "trust": "untrusted-memory",
                "sections": [{"kind": kind, "items": []} for kind, _ in section_candidates],
                "items": [], "truncated": True,
            }
        return envelope

    @staticmethod
    def _wakeup_note_item(note: dict[str, Any], kind: str) -> dict[str, Any]:
        return {
            "kind": kind,
            "role": "data",
            "trust": "untrusted-memory",
            "delimiter": "--- BEGIN UNTRUSTED MEMORY ---",
            "endDelimiter": "--- END UNTRUSTED MEMORY ---",
            "provenance": {
                "noteId": note["id"], "author": note["author"],
                "recordedAt": note["recordedAt"], "visibility": note["visibility"],
            },
            "note": note,
        }

    def _active_task_wakeup_items(self, principal: Principal) -> list[dict[str, Any]]:
        if self.hub_store is None:
            return []
        tasks = [
            task for task in self.hub_store.list_tasks(limit=500)
            if task["state"] not in TERMINAL_STATES and principal.subject in {task["fromAgent"], task["toAgent"]}
        ]
        tasks.sort(key=lambda task: (task["updatedAt"], task["taskId"]), reverse=True)
        return [
            {
                "kind": "active-task",
                "role": "data",
                "trust": "untrusted-memory",
                "delimiter": "--- BEGIN UNTRUSTED TASK LINK ---",
                "endDelimiter": "--- END UNTRUSTED TASK LINK ---",
                "provenance": {"taskId": task["taskId"], "conversationId": task["conversationId"], "updatedAt": task["updatedAt"]},
                "task": {
                    key: task[key]
                    for key in ("taskId", "conversationId", "fromAgent", "toAgent", "intent", "state", "createdAt", "updatedAt")
                },
            }
            for task in tasks[:20]
        ]

    @staticmethod
    def _task_log_note_id(operation_id: str) -> str:
        return f"mem_{hashlib.sha256(operation_id.encode('utf-8')).hexdigest()[:32]}"

    @staticmethod
    def _contains_secret_marker(value: Any) -> bool:
        serialized = json_dumps(value).casefold()
        return any(marker in serialized for marker in ('"token', 'secret', 'password', 'api_key', 'apikey'))

    def replay_terminal_outbox(
        self,
        hub_store: Any,
        *,
        max_payload_bytes: int = 65_536,
        failpoint: str | Callable[[str], None] | None = None,
    ) -> dict[str, int]:
        if not self.enable_task_log:
            return {"created": 0, "suppressed": 0, "pending": len(hub_store.list_terminal_outbox())}
        self.init()
        created = 0
        suppressed = 0
        for outbox in hub_store.list_terminal_outbox():
            task = hub_store.get_task(outbox["taskId"])
            if not task:
                continue
            terminal_state = outbox["terminalState"]
            raw_payload = task["payload"]
            safe_payload = {
                "taskId": task["taskId"],
                "conversationId": task["conversationId"],
                "intent": task["intent"],
                "state": terminal_state,
                "fromAgent": task["fromAgent"],
                "toAgent": task["toAgent"],
                "eventSequence": outbox["eventSequence"],
                "createdAt": task["createdAt"],
                "updatedAt": task["updatedAt"],
            }
            encoded = json_dumps(raw_payload).encode("utf-8")
            eligible = (
                terminal_state in {"completed", "failed", "canceled"}
                and task["intent"] in self.task_log_intents
                and len(encoded) <= max_payload_bytes
                and not self._contains_secret_marker(raw_payload)
            )
            trace_id = f"trace_tasklog_{hashlib.sha256(outbox['operationId'].encode()).hexdigest()[:16]}"
            if not eligible:
                hub_store.acknowledge_terminal_outbox(outbox["operationId"], acknowledged_at=self.now())
                self.record_receipt(
                    trace_id, "task-log", outbox["operationId"], "local.operator", "suppressed",
                    {"terminalState": terminal_state, "count": 0},
                )
                suppressed += 1
                continue
            note_id = self._task_log_note_id(outbox["operationId"])
            path = note_path(self.root, note_id)
            if path.exists():
                note = parse_note(path.read_bytes())
            else:
                note = _validate_note(
                    {
                        "schema": NOTE_SCHEMA,
                        "id": note_id,
                        "type": "task-log",
                        "title": f"Task {task['taskId']} {terminal_state}",
                        "author": "local.operator",
                        "visibility": "private",
                        "recordedAt": outbox["createdAt"],
                        "source": {"kind": "task-event", "taskId": task["taskId"], "eventSeq": outbox["eventSequence"]},
                        "participants": sorted({task["fromAgent"], task["toAgent"]}),
                        "body": json_dumps(safe_payload),
                    }
                )
                _hit_failpoint(failpoint, "before_tasklog_write")
                atomic_write(path, serialize_note(note))
                created += 1
            operation_id = self._job_operation(note, path)
            with self._connect(self.ops_path) as conn:
                conn.execute(
                    "INSERT INTO jobs(operation_id, note_id, state, created_at, updated_at) VALUES (?, ?, 'pending', ?, ?) ON CONFLICT(operation_id) DO UPDATE SET state='pending', updated_at=excluded.updated_at",
                    (operation_id, note_id, self.now(), self.now()),
                )
            self.process_jobs()
            self.record_receipt(trace_id, "task-log", outbox["operationId"], "local.operator", "committed", {"noteId": note_id, "terminalState": terminal_state})
            _hit_failpoint(failpoint, "after_tasklog_write_before_ack")
            hub_store.acknowledge_terminal_outbox(outbox["operationId"], acknowledged_at=self.now())
        return {"created": created, "suppressed": suppressed, "pending": len(hub_store.list_terminal_outbox())}

    def _allocate_note_id(self) -> str:
        for _ in range(100):
            candidate = self.new_note_id()
            if not NOTE_ID.fullmatch(candidate):
                raise MemoryError("note id generator returned an invalid id")
            with self._connect(self.ops_path) as conn:
                reserved = conn.execute("SELECT 1 FROM idempotency WHERE note_id = ?", (candidate,)).fetchone()
            if not reserved and not note_path(self.root, candidate).exists():
                return candidate
        raise ConflictError("unable to allocate a unique note id")

    @staticmethod
    def _revision(note: dict[str, Any]) -> str:
        return "sha256:" + hashlib.sha256(serialize_note(note)).hexdigest()

    def _job_operation(self, note: dict[str, Any], path: Path) -> str:
        relative = str(path.resolve().relative_to(self.root.resolve())).replace("\\", "/")
        locator = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
        return f"index:{note['id']}:{self._revision(note)}:{locator}"

    def _read_authoritative(self, note_id: str) -> dict[str, Any]:
        note, _ = self._read_authoritative_with_path(note_id)
        return note

    def _read_authoritative_with_path(self, note_id: str) -> tuple[dict[str, Any], Path]:
        NOTE_ID.fullmatch(note_id) or (_ for _ in ()).throw(MemoryError("invalid note id"))
        cached = self._authoritative_catalog.get(note_id.casefold())
        if cached is not None and cached.is_file():
            validate_existing_path(self.root, cached)
            note = parse_note(cached.read_bytes())
            if note["id"] != note_id:
                raise QuarantineError("authoritative note id changed")
            return note, cached
        candidates: list[Path] = []
        canonical = note_path(self.root, note_id)
        if canonical.is_file():
            candidates.append(canonical)
        if self.index_path.exists():
            with self._connect(self.index_path) as conn:
                row = conn.execute("SELECT relative_path FROM manifest WHERE note_id=?", (note_id,)).fetchone()
            if row:
                indexed = self.root / row["relative_path"]
                if indexed.is_file() and indexed.resolve() not in {item.resolve() for item in candidates}:
                    candidates.append(indexed)
        notes_root = self.root / "notes"
        if notes_root.exists():
            for path in sorted(notes_root.glob("**/*.md")):
                if path.resolve() in {item.resolve() for item in candidates}:
                    continue
                try:
                    parsed = parse_note(path.read_bytes())
                except QuarantineError:
                    continue
                if parsed["id"] == note_id:
                    candidates.append(path)
        valid: list[tuple[dict[str, Any], Path]] = []
        for path in candidates:
            validate_existing_path(self.root, path)
            note = parse_note(path.read_bytes())
            if note["id"] == note_id:
                valid.append((note, path.resolve()))
        if not valid:
            raise KeyError(f"note not found: {note_id}")
        if len(valid) != 1:
            raise QuarantineError("duplicate note id")
        self._authoritative_catalog[note_id.casefold()] = valid[0][1]
        return valid[0]

    def read_note(self, note_id: str, principal: Principal) -> dict[str, Any]:
        note = self._read_authoritative(note_id)
        if not self._can_read(principal, note):
            raise KeyError(f"note not found: {note_id}")
        return note

    def remove_derived_note(self, note_id: str, principal: Principal) -> None:
        """Remove only a deriver-produced note and its rebuildable indexes."""
        if not (principal.has("memory.admin") or principal.has("hub.admin")):
            raise AuthorizationError("memory.admin scope required")
        note, path = self._read_authoritative_with_path(note_id)
        if not any(
            item.get("type") == "x-derived-from" and item.get("target", "").startswith("artifact:")
            for item in note.get("relations") or []
        ):
            raise AuthorizationError("only derived notes can be removed through this operation")
        path.unlink()
        self._authoritative_catalog.pop(note_id.casefold(), None)
        self._remove_index_ids({note_id.casefold()})
        with self._connect(self.ops_path) as conn:
            conn.execute("DELETE FROM jobs WHERE note_id=?", (note_id,))
            conn.execute("DELETE FROM idempotency WHERE note_id=?", (note_id,))
        if self.search_provider is not None:
            try:
                self.search_provider.delete(note_id)
            except Exception:
                pass

    def recover_jobs(self) -> int:
        valid, collided_ids = self._scan_notes()
        return self._recover_jobs_from_scan(valid, collided_ids)

    def _recover_jobs_from_scan(
        self,
        valid: dict[str, tuple[dict[str, Any], Path]],
        collided_ids: set[str],
    ) -> int:
        count = 0
        if collided_ids:
            self._remove_index_ids(collided_ids)
        with self._connect(self.index_path) as conn:
            indexed_manifest = {
                row["note_id"]: (row["relative_path"], row["content_hash"])
                for row in conn.execute("SELECT note_id, relative_path, content_hash FROM manifest")
            }
        with self._connect(self.ops_path) as conn:
            for note, path in valid.values():
                operation_id = self._job_operation(note, path)
                relative = str(path.resolve().relative_to(self.root.resolve())).replace("\\", "/")
                indexed = indexed_manifest.get(note["id"]) == (relative, self._revision(note))
                if indexed:
                    result = conn.execute(
                        "INSERT OR IGNORE INTO jobs(operation_id, note_id, state, created_at, updated_at) VALUES (?, ?, 'done', ?, ?)",
                        (operation_id, note["id"], self.now(), self.now()),
                    )
                else:
                    result = conn.execute(
                        """
                        INSERT INTO jobs(operation_id, note_id, state, created_at, updated_at)
                        VALUES (?, ?, 'pending', ?, ?)
                        ON CONFLICT(operation_id) DO UPDATE SET state='pending', updated_at=excluded.updated_at
                        """,
                        (operation_id, note["id"], self.now(), self.now()),
                    )
                count += result.rowcount
        return count

    def _scan_notes(self) -> tuple[dict[str, tuple[dict[str, Any], Path]], set[str]]:
        records: list[tuple[dict[str, Any], Path, str, str]] = []
        active_quarantine_paths: set[str] = set()
        notes_root = self.root / "notes"
        for path in sorted(notes_root.glob("**/*.md")) if notes_root.exists() else []:
            try:
                note = parse_note(path.read_bytes())
                records.append((note, path.resolve(), note["id"].casefold(), path_collision_key(self.root, path)))
            except Exception as exc:
                self._record_quarantine(path, str(exc))
                active_quarantine_paths.add(str(path.resolve().relative_to(self.root.resolve())))
        by_id: dict[str, list[tuple[dict[str, Any], Path, str, str]]] = {}
        by_path: dict[str, list[tuple[dict[str, Any], Path, str, str]]] = {}
        for record in records:
            by_id.setdefault(record[2], []).append(record)
            by_path.setdefault(record[3], []).append(record)
        collision_records = {
            (record[1], record[2])
            for group in [*by_id.values(), *by_path.values()]
            if len(group) > 1
            for record in group
        }
        collided_ids = {record[2] for record in records if (record[1], record[2]) in collision_records}
        valid: dict[str, tuple[dict[str, Any], Path]] = {}
        for note, path, id_key, _ in records:
            if (path, id_key) in collision_records:
                self._record_quarantine(path, "duplicate note id or normalized path collision")
                active_quarantine_paths.add(str(path.resolve().relative_to(self.root.resolve())))
            else:
                valid[id_key] = (note, path)
        with self._connect(self.ops_path) as conn:
            if active_quarantine_paths:
                placeholders = ",".join("?" for _ in active_quarantine_paths)
                conn.execute(
                    f"UPDATE quarantine SET state='resolved', resolved_at=? WHERE state='active' AND path NOT IN ({placeholders})",
                    (self.now(), *sorted(active_quarantine_paths)),
                )
            else:
                conn.execute("UPDATE quarantine SET state='resolved', resolved_at=? WHERE state='active'", (self.now(),))
        self._authoritative_catalog = {key: path for key, (_, path) in valid.items()}
        return valid, collided_ids

    def _record_quarantine(self, path: Path, reason: str) -> None:
        with self._connect(self.ops_path) as conn:
            conn.execute(
                "INSERT INTO quarantine(path, reason, observed_at, resolved_at, state) VALUES (?, ?, ?, NULL, 'active') ON CONFLICT(path) DO UPDATE SET reason=excluded.reason, observed_at=excluded.observed_at, resolved_at=NULL, state='active'",
                (str(path.resolve().relative_to(self.root.resolve())), reason, self.now()),
            )

    def _remove_index_ids(self, note_ids: set[str]) -> None:
        with self._connect(self.index_path) as conn:
            for note_id in note_ids:
                conn.execute("DELETE FROM relations WHERE lower(note_id)=?", (note_id,))
                conn.execute("DELETE FROM notes WHERE lower(note_id)=?", (note_id,))
                conn.execute("DELETE FROM manifest WHERE lower(note_id)=?", (note_id,))
                try:
                    conn.execute("DELETE FROM notes_fts WHERE lower(note_id)=?", (note_id,))
                except sqlite3.OperationalError:
                    pass

    def process_jobs(self, *, failpoint: str | Callable[[str], None] | None = None) -> int:
        self.init_without_recovery()
        processed = 0
        while True:
            with self._connect(self.ops_path) as conn:
                row = conn.execute("SELECT * FROM jobs WHERE state = 'pending' ORDER BY created_at, operation_id LIMIT 1").fetchone()
                if not row:
                    break
                conn.execute("UPDATE jobs SET state='running', attempts=attempts+1, updated_at=? WHERE operation_id=?", (self.now(), row["operation_id"]))
            try:
                note, path = self._read_authoritative_with_path(row["note_id"])
                self._upsert_index(note, path=path, failpoint=failpoint)
            except Exception as exc:
                with self._connect(self.ops_path) as conn:
                    conn.execute("UPDATE jobs SET state='quarantined', updated_at=? WHERE operation_id=?", (self.now(), row["operation_id"]))
                    conn.execute(
                        "INSERT INTO quarantine(path, reason, observed_at, resolved_at, state) VALUES (?, ?, ?, NULL, 'active') ON CONFLICT(path) DO UPDATE SET reason=excluded.reason, observed_at=excluded.observed_at, resolved_at=NULL, state='active'",
                        (row["note_id"], str(exc), self.now()),
                    )
            else:
                with self._connect(self.ops_path) as conn:
                    conn.execute("UPDATE jobs SET state='done', updated_at=? WHERE operation_id=?", (self.now(), row["operation_id"]))
                processed += 1
        return processed

    def init_without_recovery(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._init_index()

    def _upsert_index(
        self,
        note: dict[str, Any],
        *,
        path: Path | None = None,
        index_path: Path | None = None,
        prior: tuple[int, str] | None = None,
        failpoint: str | Callable[[str], None] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        path = path or note_path(self.root, note["id"])
        validate_existing_path(self.root, path)
        content_hash = self._revision(note)
        relative = str(path.resolve().relative_to(self.root.resolve())).replace("\\", "/")
        target_index = index_path or self.index_path
        with (nullcontext(connection) if connection is not None else self._connect(target_index)) as conn:
            existing = conn.execute("SELECT revision, content_hash FROM manifest WHERE note_id=?", (note["id"],)).fetchone()
            previous_revision = existing["revision"] if existing else (prior[0] if prior else 0)
            previous_hash = existing["content_hash"] if existing else (prior[1] if prior else None)
            revision = previous_revision if previous_hash == content_hash else previous_revision + 1
            conn.execute(
                """
                INSERT INTO notes(
                    note_id, relative_path, author, visibility, title, body, recorded_at,
                    content_hash, revision, note_type, project, participants_json, about_json, supersedes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    relative_path=excluded.relative_path, author=excluded.author,
                    visibility=excluded.visibility, title=excluded.title, body=excluded.body,
                    recorded_at=excluded.recorded_at, content_hash=excluded.content_hash,
                    revision=excluded.revision, note_type=excluded.note_type,
                    project=excluded.project, participants_json=excluded.participants_json,
                    about_json=excluded.about_json, supersedes=excluded.supersedes
                """,
                (
                    note["id"], relative, note["author"], note["visibility"], note["title"], note["body"],
                    note["recordedAt"], content_hash, revision, note["type"], note.get("project"),
                    json_dumps(note.get("participants") or []), json_dumps(note.get("about") or []), note.get("supersedes"),
                ),
            )
            _hit_failpoint(failpoint, "after_index_upsert_before_manifest")
            conn.execute(
                "INSERT INTO manifest VALUES (?, ?, ?, ?) ON CONFLICT(note_id) DO UPDATE SET relative_path=excluded.relative_path, content_hash=excluded.content_hash, revision=excluded.revision",
                (note["id"], relative, content_hash, revision),
            )
            conn.execute("DELETE FROM relations WHERE note_id=?", (note["id"],))
            conn.executemany("INSERT INTO relations VALUES (?, ?, ?)", [(note["id"], item["type"], item["target"]) for item in note.get("relations") or []])
            try:
                conn.execute("DELETE FROM notes_fts WHERE note_id=?", (note["id"],))
                conn.execute("INSERT INTO notes_fts(note_id,title,body) VALUES (?, ?, ?)", (note["id"], note["title"], note["body"]))
            except sqlite3.OperationalError:
                pass

    def search(self, query: str, principal: Principal, *, limit: int = 50, mode: str = "auto") -> list[dict[str, Any]]:
        if not principal.has("memory.read"):
            return []
        if mode not in {"auto", "hybrid", "keyword"}:
            raise MemoryError("search mode must be auto, hybrid, or keyword")
        if mode != "keyword" and self.search_provider is not None:
            try:
                results = self.search_provider.search(
                    query, principal, load_note=self._read_authoritative,
                    can_read=self._can_read, limit=max(1, min(limit, 100)),
                )
                self.search_provider.last_fallback_reason = None
                return results
            except Exception as exc:
                self.search_provider.last_fallback_reason = type(exc).__name__
        bounded_limit = max(1, min(limit, 100))
        terms = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
        fts_query = " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
        visibility_sql = "1=1" if principal.has("memory.admin") else "(n.author=? OR n.visibility='shared' OR n.visibility=?)"
        visibility_args: tuple[Any, ...] = () if principal.has("memory.admin") else (principal.subject, f"direct:{principal.subject}")
        with self._connect(self.index_path) as conn:
            try:
                if not fts_query:
                    raise sqlite3.OperationalError("empty sanitized FTS query")
                rows = conn.execute(
                    f"SELECT f.note_id FROM notes_fts f JOIN notes n ON n.note_id=f.note_id WHERE notes_fts MATCH ? AND {visibility_sql} ORDER BY rank, f.note_id LIMIT ?",
                    (fts_query, *visibility_args, bounded_limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = conn.execute(
                    f"SELECT n.note_id FROM notes n WHERE (n.title LIKE ? OR n.body LIKE ?) AND {visibility_sql} ORDER BY n.recorded_at DESC, n.note_id LIMIT ?",
                    (f"%{query}%", f"%{query}%", *visibility_args, bounded_limit),
                ).fetchall()
        results = []
        for row in rows:
            try:
                results.append(self.read_note(row["note_id"], principal))
            except (KeyError, QuarantineError):
                continue
        return results

    def rebuild_search_index(self, *, fail_after_chunks: int | None = None) -> dict[str, Any]:
        if self.search_provider is None:
            raise MemoryError("hybrid retrieval is not configured")
        valid, collided_ids = self._scan_notes()
        notes = [note for key, (note, _) in sorted(valid.items()) if key not in collided_ids]
        return self.search_provider.rebuild(
            notes, source_revision=self.source_revision,
            fail_after_chunks=fail_after_chunks,
        )

    def search_status(self) -> dict[str, Any]:
        if self.search_provider is None:
            return {"provider": "keyword", "mode": "keyword", "fallback": True}
        return self.search_provider.status()

    def _superseded_ids(self, principal: Principal) -> set[str]:
        """Return only caller-visible, authority-valid temporal effects."""
        valid, _ = self._scan_notes()
        superseded: set[str] = set()
        for successor, _ in valid.values():
            target = successor.get("supersedes")
            if not target:
                continue
            try:
                visible_successor = self.read_note(successor["id"], principal)
                previous = self.read_note(target, principal)
            except (KeyError, QuarantineError):
                continue
            if visible_successor["author"] == previous["author"] or visible_successor["author"] == "local.operator":
                superseded.add(target)
        return superseded

    def timeline(
        self,
        principal: Principal,
        *,
        project: str | None = None,
        pair: tuple[str, str] | None = None,
        about: str | None = None,
        include_superseded: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not principal.has("memory.read"):
            raise AuthorizationError("memory.read scope required")
        with self._connect(self.index_path) as conn:
            rows = conn.execute("SELECT note_id FROM notes ORDER BY recorded_at DESC, note_id DESC").fetchall()
        superseded = self._superseded_ids(principal)
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                note = self.read_note(row["note_id"], principal)
            except (KeyError, QuarantineError):
                continue
            if not include_superseded and note["id"] in superseded:
                continue
            if project is not None and note.get("project") != project:
                continue
            if about is not None and about not in (note.get("about") or []):
                continue
            if pair is not None:
                actors = {note["author"], *(note.get("participants") or []), *(note.get("about") or [])}
                if note["visibility"].startswith("direct:"):
                    actors.add(note["visibility"][7:])
                if not set(pair) <= actors:
                    continue
            item = dict(note)
            item["temporalStatus"] = "superseded" if note["id"] in superseded else "current"
            items.append(item)
            if len(items) >= max(1, min(limit, 100)):
                break
        return items

    @staticmethod
    def _graph_edges_for_note(note: dict[str, Any]) -> list[dict[str, str]]:
        source = f"note:{note['id']}"
        edges = [{"source": source, "type": "authored_by", "target": f"agent:{note['author']}"}]
        if note.get("project"):
            edges.append({"source": source, "type": "project", "target": f"project:{note['project']}"})
        for subject in note.get("participants") or []:
            edges.append({"source": source, "type": "participant", "target": f"agent:{subject}"})
        for subject in note.get("about") or []:
            edges.append({"source": source, "type": "about", "target": f"agent:{subject}"})
        if note.get("supersedes"):
            edges.append({"source": source, "type": "supersedes", "target": f"note:{note['supersedes']}"})
        for relation in note.get("relations") or []:
            target = relation["target"]
            if ":" not in target and PRINCIPAL_ID.fullmatch(target):
                target = f"agent:{target}"
            edges.append({"source": source, "type": relation["type"], "target": target})
        return edges

    def graph(self, principal: Principal, node: str, *, hops: int = 1) -> dict[str, Any]:
        if not principal.has("memory.read"):
            raise AuthorizationError("memory.read scope required")
        if hops not in {1, 2}:
            raise MemoryError("graph hops must be 1 or 2")
        with self._connect(self.index_path) as conn:
            rows = conn.execute("SELECT note_id FROM notes ORDER BY note_id").fetchall()
        authorized: list[tuple[dict[str, Any], list[dict[str, str]]]] = []
        for row in rows:
            try:
                note = self.read_note(row["note_id"], principal)
            except (KeyError, QuarantineError):
                continue
            safe_edges: list[dict[str, str]] = []
            for edge in self._graph_edges_for_note(note):
                target = edge["target"]
                if target.startswith("note:"):
                    target_id = target[5:]
                    try:
                        self.read_note(target_id, principal)
                    except (KeyError, QuarantineError, MemoryError):
                        continue
                safe_edges.append(edge)
            authorized.append((note, safe_edges))
        frontier = {node}
        selected: list[tuple[dict[str, Any], dict[str, str]]] = []
        seen_edges: set[tuple[str, str, str, str]] = set()
        for _ in range(hops):
            next_frontier: set[str] = set()
            for note, edges in authorized:
                for edge in edges:
                    if edge["source"] in frontier or edge["target"] in frontier:
                        key = (note["id"], edge["source"], edge["type"], edge["target"])
                        if key not in seen_edges:
                            selected.append((note, edge))
                            seen_edges.add(key)
                        next_frontier.update({edge["source"], edge["target"]})
            frontier = next_frontier
        nodes = sorted({*(edge["source"] for _, edge in selected), *(edge["target"] for _, edge in selected)})
        return {
            "nodes": [{"id": value, "role": "data"} for value in nodes],
            "edges": [{**edge, "noteId": note["id"], "recordedAt": note["recordedAt"]} for note, edge in selected],
        }

    def stats(self, principal: Principal) -> dict[str, Any]:
        if not principal.has("memory.admin"):
            raise AuthorizationError("memory.admin scope required")
        self.init()
        with self._connect(self.ops_path) as conn:
            queue_depth = int(conn.execute("SELECT COUNT(*) FROM jobs WHERE state IN ('pending','running')").fetchone()[0])
            quarantine_count = int(conn.execute("SELECT COUNT(*) FROM quarantine WHERE state='active'").fetchone()[0])
            delivery_backlog = int(conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])
            receipt_count = int(conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0])
        with self._connect(self.index_path) as conn:
            note_count = int(conn.execute("SELECT COUNT(*) FROM manifest").fetchone()[0])
            index_revision = int(conn.execute("SELECT COALESCE(MAX(revision),0) FROM manifest").fetchone()[0])
        degraded = []
        if queue_depth:
            degraded.append("jobs-pending")
        if quarantine_count:
            degraded.append("quarantine-nonempty")
        return {
            "queueDepth": queue_depth,
            "indexRevision": index_revision,
            "indexedNotes": note_count,
            "quarantineCount": quarantine_count,
            "deliveryBacklog": delivery_backlog,
            "receiptCount": receipt_count,
            "degraded": degraded,
        }

    def source_revision(self, note_id: str) -> int:
        return int(self.note_consistency(note_id)["sourceRevision"])

    def note_consistency(self, note_id: str) -> dict[str, Any]:
        note = self._read_authoritative(note_id)
        source_hash = self._revision(note)
        with self._connect(self.index_path) as conn:
            row = conn.execute("SELECT revision, content_hash FROM manifest WHERE note_id=?", (note_id,)).fetchone()
        indexed_revision = int(row["revision"]) if row else 0
        indexed_hash = row["content_hash"] if row else None
        source_revision = indexed_revision if indexed_hash == source_hash else indexed_revision + 1
        return {
            "sourceRevision": source_revision,
            "sourceHash": source_hash,
            "indexedRevision": indexed_revision,
            "indexedHash": indexed_hash,
            "consistency": "current" if indexed_hash == source_hash else "stale",
        }

    def index_status(self) -> dict[str, Any]:
        valid, collided_ids = self._scan_notes()
        statuses = [self.note_consistency(note["id"]) for note, _ in valid.values() if note["id"].casefold() not in collided_ids]
        source_revision = max((item["sourceRevision"] for item in statuses), default=0)
        indexed_revision = max((item["indexedRevision"] for item in statuses), default=0)
        degraded = ["index-stale"] if any(item["consistency"] == "stale" for item in statuses) else []
        return {
            "sourceRevision": source_revision,
            "indexedRevision": indexed_revision,
            "consistency": "eventual",
            "degraded": degraded,
        }

    def rebuild_index(self, *, failpoint: str | Callable[[str], None] | None = None) -> int:
        if not self.ops_path.exists():
            self.init()
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        old_manifest: dict[str, tuple[int, str]] = {}
        if self.index_path.exists():
            with self._connect(self.index_path) as conn:
                old_manifest = {row["note_id"]: (row["revision"], row["content_hash"]) for row in conn.execute("SELECT * FROM manifest")}
        generation = self.root / f".index-generation-{uuid.uuid4().hex}.sqlite"
        self._init_index(generation)
        count = 0
        valid, _ = self._scan_notes()
        try:
            with self._connect(generation) as generation_conn:
                for note, path in valid.values():
                    self._upsert_index(
                        note,
                        path=path,
                        index_path=generation,
                        prior=old_manifest.get(note["id"]),
                        connection=generation_conn,
                    )
                    count += 1
                    _hit_failpoint(failpoint, "during_index_generation")
            _hit_failpoint(failpoint, "before_index_generation_swap")
            os.replace(generation, self.index_path)
            _hit_failpoint(failpoint, "after_index_generation_swap")
            return count
        finally:
            if generation.exists():
                generation.unlink()

    def _assign_missing_ids(self, *, failpoint: str | Callable[[str], None] | None = None) -> int:
        if not self.enable_watcher_side_effects:
            return 0
        yaml = _yaml_module()
        assigned = 0
        notes_root = self.root / "notes"
        existing_ids: set[str] = set()
        for path in sorted(notes_root.glob("**/*.md")) if notes_root.exists() else []:
            try:
                existing_ids.add(parse_note(path.read_bytes())["id"])
            except QuarantineError:
                pass
        for path in sorted(notes_root.glob("**/*.md")) if notes_root.exists() else []:
            data = path.read_bytes()
            try:
                parse_note(data)
                continue
            except QuarantineError:
                pass
            try:
                text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                if not text.startswith("---\n") or "\n---\n" not in text[4:]:
                    continue
                header, body = text[4:].split("\n---\n", 1)
                metadata = yaml.safe_load(header)
                if not isinstance(metadata, dict) or "id" in metadata or metadata.get("author") != "local.operator":
                    continue
                validate_existing_path(self.root, path)
                relative = str(path.resolve().relative_to(notes_root.resolve())).replace("\\", "/")
                salt = 0
                while True:
                    candidate_seed = f"local-admin\0{relative}\0{salt}\0".encode("utf-8") + data
                    candidate = f"mem_{hashlib.sha256(candidate_seed).hexdigest()[:32]}"
                    if candidate not in existing_ids and not note_path(self.root, candidate).exists():
                        break
                    salt += 1
                metadata["id"] = candidate
                metadata["body"] = body
                note = _validate_note(metadata)
                _hit_failpoint(failpoint, "before_missing_id_rewrite")
                atomic_write(path, serialize_note(note))
                _hit_failpoint(failpoint, "after_missing_id_rewrite")
                existing_ids.add(candidate)
                assigned += 1
            except Exception:
                continue
        return assigned

    def sync_filesystem(self, *, failpoint: str | Callable[[str], None] | None = None) -> dict[str, int]:
        """Deterministic watcher cycle: quarantine, enqueue, index, and prune deletes."""
        self.init()
        assigned = self._assign_missing_ids(failpoint=failpoint)
        enqueued = self.recover_jobs()
        indexed = self.process_jobs()
        valid, collided_ids = self._scan_notes()
        current = {note["id"] for note, _ in valid.values()}
        if collided_ids:
            self._remove_index_ids(collided_ids)
        with self._connect(self.index_path) as conn:
            indexed_ids = {row[0] for row in conn.execute("SELECT note_id FROM manifest").fetchall()}
            removed = indexed_ids - current
            for note_id in removed:
                conn.execute("DELETE FROM relations WHERE note_id=?", (note_id,))
                conn.execute("DELETE FROM notes WHERE note_id=?", (note_id,))
                conn.execute("DELETE FROM manifest WHERE note_id=?", (note_id,))
                try:
                    conn.execute("DELETE FROM notes_fts WHERE note_id=?", (note_id,))
                except sqlite3.OperationalError:
                    pass
        if self.enable_delivery:
            self._generate_all_deliveries(valid)
        return {"assigned": assigned, "enqueued": enqueued, "indexed": indexed, "removed": len(removed)}

    def quarantine(self) -> list[dict[str, str]]:
        with self._connect(self.ops_path) as conn:
            rows = conn.execute("SELECT path, reason, observed_at, resolved_at, state FROM quarantine ORDER BY path").fetchall()
        return [dict(row) for row in rows]


class MemoryWatcher:
    """Deterministic debounce queue with a watchdog-compatible callback seam."""

    def __init__(self, service: MemoryService, *, clock: Callable[[], float] = time.monotonic, debounce_seconds: float = 0.25):
        self.service = service
        self.clock = clock
        self.debounce_seconds = max(0.0, debounce_seconds)
        self._pending: dict[str, float] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def notify(self, path: str | Path, event_type: str, *, dest_path: str | Path | None = None) -> None:
        if event_type not in {"created", "modified", "moved", "deleted"}:
            return
        due = self.clock() + self.debounce_seconds
        for candidate in (path, dest_path):
            if candidate is None:
                continue
            raw = unicodedata.normalize("NFC", str(candidate)).replace("\\", "/").casefold()
            self._pending[raw] = due

    def on_any_event(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        self.notify(
            getattr(event, "src_path"),
            getattr(event, "event_type", "modified"),
            dest_path=getattr(event, "dest_path", None),
        )

    def flush(self, *, force: bool = False) -> dict[str, int] | None:
        now = self.clock()
        ready = [key for key, due in self._pending.items() if force or due <= now]
        if not ready:
            return None
        for key in ready:
            self._pending.pop(key, None)
        return self.service.sync_filesystem()

    def scan_once(self) -> dict[str, int]:
        """Polling fallback and explicit full-scan recovery seam."""
        return self.service.sync_filesystem()

    def startup_scan(self) -> dict[str, int]:
        return self.service.sync_filesystem()
