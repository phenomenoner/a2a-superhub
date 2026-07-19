from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from .models import TERMINAL_STATES, ensure_object, ensure_state, ensure_transition, json_dumps, json_loads, new_id, utc_now


class HubStore:
    """Durable task, event, and agent registry store."""

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "tasks" / "hub-tasks.sqlite"

    def init(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    card_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    idempotency_key TEXT UNIQUE,
                    from_agent TEXT NOT NULL,
                    to_agent TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    artifact_refs_json TEXT NOT NULL,
                    permissions_json TEXT NOT NULL,
                    limits_json TEXT NOT NULL,
                    correlation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT,
                    payload_json TEXT NOT NULL,
                    sequence INTEGER,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
                );
                CREATE TABLE IF NOT EXISTS terminal_outbox (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    terminal_state TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT,
                    FOREIGN KEY(task_id) REFERENCES tasks(task_id),
                    UNIQUE(task_id, event_sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_to_state ON tasks(to_agent, state);
                """
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "sequence" not in columns:
                conn.execute("ALTER TABLE events ADD COLUMN sequence INTEGER")
            task_ids = conn.execute("SELECT DISTINCT task_id FROM events WHERE sequence IS NULL").fetchall()
            for task_row in task_ids:
                rows = conn.execute(
                    "SELECT event_id FROM events WHERE task_id = ? ORDER BY created_at, rowid",
                    (task_row[0],),
                ).fetchall()
                for sequence, event_row in enumerate(rows, start=1):
                    conn.execute("UPDATE events SET sequence = ? WHERE event_id = ?", (sequence, event_row[0]))
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_task_sequence ON events(task_id, sequence)")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_agent(self, card: dict[str, Any]) -> dict[str, Any]:
        self.init()
        card = ensure_object(card, name="card")
        agent_id = str(card.get("id") or card.get("name") or "").strip()
        if not agent_id:
            raise ValueError("agent card requires id or name")
        now = utc_now()
        with self.connect() as conn:
            existing = conn.execute("SELECT created_at FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            created_at = existing["created_at"] if existing else now
            conn.execute(
                """
                INSERT INTO agents(agent_id, card_json, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    card_json = excluded.card_json,
                    updated_at = excluded.updated_at
                """,
                (agent_id, json_dumps(card), created_at, now),
            )
        return {"agentId": agent_id, "card": card, "createdAt": created_at, "updatedAt": now}

    def list_agents(self) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY agent_id").fetchall()
        return [self._agent_from_row(row) for row in rows]

    def create_task(self, task: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self.init()
        task = ensure_object(task, name="task")
        idempotency_key = task.get("idempotencyKey") or task.get("idempotency_key")
        if idempotency_key:
            existing = self.get_task_by_idempotency(str(idempotency_key))
            if existing:
                return existing, False
        task_id = str(task.get("taskId") or task.get("task_id") or new_id("task"))
        conversation_id = str(task.get("conversationId") or task.get("conversation_id") or new_id("conv"))
        from_agent = str(task.get("fromAgent") or task.get("from_agent") or "").strip()
        to_agent = str(task.get("toAgent") or task.get("to_agent") or "").strip()
        intent = str(task.get("intent") or "agent.query").strip()
        if not from_agent or not to_agent:
            raise ValueError("task requires fromAgent and toAgent")
        state = ensure_state(str(task.get("state") or "submitted"))
        now = utc_now()
        record = {
            "taskId": task_id,
            "conversationId": conversation_id,
            "idempotencyKey": idempotency_key,
            "fromAgent": from_agent,
            "toAgent": to_agent,
            "intent": intent,
            "state": state,
            "payload": task.get("payload") or {},
            "artifactRefs": task.get("artifactRefs") or task.get("artifact_refs") or [],
            "permissions": task.get("permissions") or {"sideEffects": "default-deny", "scopes": []},
            "limits": task.get("limits") or {},
            "correlation": task.get("correlation") or {},
            "createdAt": now,
            "updatedAt": now,
        }
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, conversation_id, idempotency_key, from_agent, to_agent, intent, state,
                    payload_json, artifact_refs_json, permissions_json, limits_json, correlation_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    conversation_id,
                    idempotency_key,
                    from_agent,
                    to_agent,
                    intent,
                    state,
                    json_dumps(record["payload"]),
                    json_dumps(record["artifactRefs"]),
                    json_dumps(record["permissions"]),
                    json_dumps(record["limits"]),
                    json_dumps(record["correlation"]),
                    now,
                    now,
                ),
            )
            self._append_event_conn(conn, task_id, "task.submitted", state, {"task": record}, now)
        return record, True

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._task_from_row(row) if row else None

    def get_task_by_idempotency(self, key: str) -> dict[str, Any] | None:
        self.init()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE idempotency_key = ?", (key,)).fetchone()
        return self._task_from_row(row) if row else None

    def list_tasks(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.init()
        limit = max(1, min(int(limit), 500))
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._task_from_row(row) for row in rows]

    def append_event(
        self,
        task_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        state: str | None = None,
        failpoint: str | Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        self.init()
        if state is not None:
            ensure_state(state)
        now = utc_now()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            task_row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not task_row:
                raise KeyError(f"task not found: {task_id}")
            ensure_transition(task_row["state"], state)
            event = self._append_event_conn(conn, task_id, kind, state, payload or {}, now, failpoint=failpoint)
            if state is not None:
                conn.execute("UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?", (state, now, task_id))
        self._hit_failpoint(failpoint, "after_commit_before_return")
        return event

    def list_events(self, task_id: str) -> list[dict[str, Any]]:
        self.init()
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE task_id = ? ORDER BY sequence", (task_id,)).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_terminal_outbox(self, *, pending_only: bool = True) -> list[dict[str, Any]]:
        self.init()
        where = "WHERE acknowledged_at IS NULL" if pending_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM terminal_outbox {where} ORDER BY created_at, operation_id"
            ).fetchall()
        return [
            {
                "operationId": row["operation_id"],
                "taskId": row["task_id"],
                "eventSequence": row["event_sequence"],
                "terminalState": row["terminal_state"],
                "payload": json_loads(row["payload_json"], {}),
                "createdAt": row["created_at"],
                "acknowledgedAt": row["acknowledged_at"],
            }
            for row in rows
        ]

    def acknowledge_terminal_outbox(self, operation_id: str, *, acknowledged_at: str | None = None) -> bool:
        self.init()
        with self.connect() as conn:
            result = conn.execute(
                "UPDATE terminal_outbox SET acknowledged_at = COALESCE(acknowledged_at, ?) WHERE operation_id = ?",
                (acknowledged_at or utc_now(), operation_id),
            )
        return result.rowcount == 1

    def health(self) -> dict[str, Any]:
        self.init()
        with self.connect() as conn:
            agents = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
            tasks = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "schema": "a2a-superhub.health.v1",
            "status": "ready",
            "stateDir": str(self.state_dir),
            "database": str(self.db_path),
            "counts": {"agents": agents, "tasks": tasks, "events": events},
        }

    def _append_event_conn(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        kind: str,
        state: str | None,
        payload: dict[str, Any],
        now: str,
        failpoint: str | Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        event = {
            "eventId": new_id("evt"),
            "taskId": task_id,
            "kind": kind,
            "state": state,
            "payload": payload,
            "sequence": sequence,
            "createdAt": now,
        }
        conn.execute(
            "INSERT INTO events(event_id, task_id, kind, state, payload_json, sequence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event["eventId"], task_id, kind, state, json_dumps(payload), sequence, now),
        )
        if state in TERMINAL_STATES:
            self._hit_failpoint(failpoint, "after_event_before_outbox")
            operation_id = f"tasklog:{task_id}:{sequence}"
            conn.execute(
                """
                INSERT INTO terminal_outbox(
                    operation_id, task_id, event_sequence, terminal_state, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (operation_id, task_id, sequence, state, json_dumps(payload), now),
            )
        return event

    @staticmethod
    def _hit_failpoint(failpoint: str | Callable[[str], None] | None, stage: str) -> None:
        if callable(failpoint):
            failpoint(stage)
        elif failpoint == stage:
            raise RuntimeError(f"failpoint:{stage}")

    def _task_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "taskId": row["task_id"],
            "conversationId": row["conversation_id"],
            "idempotencyKey": row["idempotency_key"],
            "fromAgent": row["from_agent"],
            "toAgent": row["to_agent"],
            "intent": row["intent"],
            "state": row["state"],
            "payload": json_loads(row["payload_json"], {}),
            "artifactRefs": json_loads(row["artifact_refs_json"], []),
            "permissions": json_loads(row["permissions_json"], {}),
            "limits": json_loads(row["limits_json"], {}),
            "correlation": json_loads(row["correlation_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "eventId": row["event_id"],
            "taskId": row["task_id"],
            "kind": row["kind"],
            "state": row["state"],
            "payload": json_loads(row["payload_json"], {}),
            "sequence": row["sequence"],
            "createdAt": row["created_at"],
        }

    def _agent_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "agentId": row["agent_id"],
            "card": json_loads(row["card_json"], {}),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }


