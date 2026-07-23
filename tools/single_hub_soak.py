"""Run the real HTTP single-hub durability gate and emit sanitized evidence."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from a2a_superhub.client import HubClient, HubClientError
from a2a_superhub.memory import atomic_write, note_path, parse_note, serialize_note


EVIDENCE_SCHEMA = "a2a-superhub.single-hub-soak.v1"
ROOT = Path(__file__).resolve().parents[1]


class SoakStopping(RuntimeError):
    """Internal normal-stop signal for a worker waiting on a restart."""


class SoakInvariantError(RuntimeError):
    """Sanitized workload invariant failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def make_pdf(text: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref}),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def raw_artifact(base: str, token: str, data: bytes, *, filename: str, media_type: str, visibility: str) -> dict[str, Any]:
    request = urllib.request.Request(
        base + "/v1/artifacts/raw",
        data=data,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": media_type,
            "X-Artifact-Filename": filename,
            "X-Artifact-Visibility": visibility,
            "X-Artifact-SHA256": hashlib.sha256(data).hexdigest(),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"artifact request failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise HubClientError(f"hub connection failed ({type(exc).__name__})", kind="connection") from None


def rss_bytes(pid: int) -> int:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        process = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
        if not process:
            return 0
        try:
            counters = Counters()
            counters.cb = ctypes.sizeof(counters)
            if not ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                return 0
            return int(counters.WorkingSetSize)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    status = Path(f"/proc/{pid}/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    return 0


class Runtime:
    def __init__(self, state: Path, principals: Path, port: int):
        self.state = state
        self.principals = principals
        self.port = port
        self.process: subprocess.Popen[str] | None = None
        self.stdout_path = self.state.parent / "server.stdout.log"
        self.stderr_path = self.state.parent / "server.stderr.log"
        self._stdout_handle: TextIO | None = None
        self._stderr_handle: TextIO | None = None
        self._output_lock = threading.Lock()
        self._expected_outage = False
        self._unexpected_exit_recorded = False

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _close_output_locked(self) -> None:
        for handle in (self._stdout_handle, self._stderr_handle):
            if handle is not None and not handle.closed:
                handle.close()
        self._stdout_handle = None
        self._stderr_handle = None

    def unexpected_exit_code(self) -> int | None:
        process = self.process
        if process is None:
            return None
        return_code = process.poll()
        if return_code is None:
            return None
        with self._output_lock:
            if self._expected_outage:
                return None
            self._close_output_locked()
            if not self._unexpected_exit_recorded:
                self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
                with self.stderr_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps({
                        "at": utc_now(),
                        "event": "unexpected-exit",
                        "returnCode": return_code,
                    }, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self._unexpected_exit_recorded = True
        return int(return_code)

    def start(self) -> None:
        environment = os.environ.copy()
        source = str(ROOT / "src")
        environment["PYTHONPATH"] = source + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        self.state.parent.mkdir(parents=True, exist_ok=True)
        with self._output_lock:
            self._close_output_locked()
            self._stdout_handle = self.stdout_path.open("a", encoding="utf-8", newline="\n")
            self._stderr_handle = self.stderr_path.open("a", encoding="utf-8", newline="\n")
            self._unexpected_exit_recorded = False
        try:
            self.process = subprocess.Popen(
                [
                    sys.executable, str(ROOT / "tools" / "_soak_server.py"),
                    "--state", str(self.state), "--principals", str(self.principals), "--port", str(self.port),
                ],
                cwd=ROOT,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                text=True,
            )
        except BaseException:
            with self._output_lock:
                self._close_output_locked()
            raise
        deadline = time.monotonic() + 30
        client = HubClient(self.base, timeout=2)
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.unexpected_exit_code()
                raise RuntimeError(f"hub process exited during startup with code {self.process.returncode}")
            try:
                if client.ready().get("status") == "ready":
                    with self._output_lock:
                        self._expected_outage = False
                    return
            except HubClientError:
                time.sleep(0.2)
        raise RuntimeError("hub process did not become ready")

    def stop(self, *, hard: bool) -> None:
        with self._output_lock:
            self._expected_outage = True
        process = self.process
        if process is None:
            with self._output_lock:
                self._close_output_locked()
            return
        if process.poll() is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            with self._output_lock:
                self._close_output_locked()
            return
        if hard:
            process.kill()
        else:
            assert process.stdin is not None
            process.stdin.write("shutdown\n")
            process.stdin.flush()
        try:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
                if not hard:
                    raise RuntimeError("graceful hub shutdown exceeded its deadline")
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            with self._output_lock:
                self._close_output_locked()


class Soak:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.workspace = Path(args.workspace).resolve()
        self.state = self.workspace / "state"
        self.principals_path = self.workspace / "principals.json"
        self.port = free_port()
        self.runtime = Runtime(self.state, self.principals_path, self.port)
        self.owner = HubClient(self.runtime.base, token="soak-owner", timeout=10)
        self.reader = HubClient(self.runtime.base, token="soak-reader", timeout=10)
        self.admin = HubClient(self.runtime.base, token="soak-admin", timeout=10)
        self.stop_event = threading.Event()
        self.failures: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.task_ids: set[str] = set()
        self.note_ids: set[str] = set()
        self.delivery_note_ids: set[str] = set()
        self.artifact_ids: set[str] = set()
        self.filesystem_writes = 0
        self.retries = 0
        self.searches = 0
        self.inbox_acks = 0
        self.authorization_checks = 0
        self.restarts = {"graceful": 0, "controlledKill": 0}
        self.rss_samples: list[int] = []
        self.state_samples: list[int] = []
        self.private_note_id = ""
        self.derived_note_id = ""

    @staticmethod
    def failure_code(exc: Exception) -> str:
        if isinstance(exc, HubClientError):
            return f"HubClientError:{exc.kind}:{exc.status or 0}:{exc.code or 'none'}"
        if isinstance(exc, SoakInvariantError):
            return f"SoakInvariantError:{exc.code}"
        return type(exc).__name__

    def prepare(self) -> None:
        if self.workspace.exists():
            raise RuntimeError("soak workspace must not already exist")
        self.workspace.mkdir(parents=True)
        principals = {
            "soak-owner": {
                "subject": "agent.alpha", "kind": "agent", "tokenId": "tok_soak_owner",
                "scopes": ["task.read", "task.write", "artifact.read", "artifact.write", "artifact.share", "memory.read", "memory.write", "memory.share"],
            },
            "soak-reader": {
                "subject": "agent.beta", "kind": "agent", "tokenId": "tok_soak_reader",
                "scopes": ["task.read", "artifact.read", "memory.read"],
            },
            "soak-admin": {
                "subject": "local.operator", "kind": "operator", "tokenId": "tok_soak_admin",
                "scopes": ["hub.admin"],
            },
        }
        self.principals_path.write_text(json.dumps(principals), encoding="utf-8")
        self.runtime.start()

        private = self.owner.create_note({
            "type": "observation", "title": "private authorization sentinel",
            "visibility": "private", "body": "SOAK-PRIVATE",
        }, "soak-private-sentinel")
        self.private_note_id = private["id"]
        self.note_ids.add(self.private_note_id)
        self.assert_reader_denied()

        pdf = make_pdf("SOAK-DERIVATION-SENTINEL")
        manifest = raw_artifact(
            self.runtime.base, "soak-owner", pdf,
            filename="soak-sentinel.pdf", media_type="application/pdf", visibility="shared",
        )
        self.artifact_ids.add(manifest["artifactId"])
        derived = self.owner.request("POST", f"/v1/artifacts/{manifest['artifactId']}/derive", body={})
        self.derived_note_id = derived["noteId"]
        self.note_ids.add(self.derived_note_id)
        found = self.reader.search("SOAK-DERIVATION-SENTINEL")
        if self.derived_note_id not in {item["id"] for item in found["items"]}:
            raise RuntimeError("derived PDF text was not provider-visible through search")

    def retry(self, operation: Callable[[], Any], *, label: str = "operation") -> Any:
        deadline = time.monotonic() + 30
        while not self.stop_event.is_set() and time.monotonic() < deadline:
            try:
                return operation()
            except HubClientError as exc:
                if exc.kind != "connection":
                    raise
                if self.runtime.unexpected_exit_code() is not None:
                    raise SoakInvariantError("hub-process-exited") from None
                with self.lock:
                    self.retries += 1
                time.sleep(0.2)
        if self.stop_event.is_set():
            raise SoakStopping("soak workload is stopping")
        raise SoakInvariantError(f"operation-timeout:{label}")

    def task_worker(self) -> None:
        sequence = 0
        while not self.stop_event.is_set():
            key = f"soak-task-{sequence}"
            response = self.retry(lambda: self.owner.create_task({
                "fromAgent": "agent.alpha", "toAgent": "agent.beta", "intent": "soak.observe",
                "idempotencyKey": key, "payload": {"summary": "durability observation"},
                "permissions": {"sideEffects": "default-deny", "scopes": []},
            }), label="task-create")
            with self.lock:
                self.task_ids.add(response["task"]["taskId"])
            sequence += 1
            self.stop_event.wait(self.args.operation_interval)

    def note_worker(self) -> None:
        sequence = 0
        while not self.stop_event.is_set():
            response = self.retry(lambda: self.owner.create_note({
                "type": "observation", "title": f"soak note {sequence}", "visibility": "shared",
                "about": ["agent.beta"], "body": "SOAK-STABLE concurrent note",
            }, f"soak-note-{sequence}"), label="note-create")
            with self.lock:
                self.note_ids.add(response["id"])
                self.delivery_note_ids.add(response["id"])
            sequence += 1
            self.stop_event.wait(self.args.operation_interval)

    def filesystem_worker(self) -> None:
        sequence = 0
        memory_root = self.state / "memory"
        while not self.stop_event.is_set():
            note_id = f"mem_{uuid.uuid4().hex}"
            note = {
                "schema": "a2a-superhub.memory.note.v1",
                "id": note_id,
                "type": "observation",
                "title": f"filesystem soak note {sequence}",
                "author": "local.operator",
                "visibility": "shared",
                "recordedAt": utc_now(),
                "source": {"kind": "filesystem"},
                "about": ["agent.beta"],
                "body": "SOAK-FILESYSTEM-STABLE concurrent direct edit",
            }
            atomic_write(note_path(memory_root, note_id), serialize_note(note))
            with self.lock:
                self.note_ids.add(note_id)
                self.delivery_note_ids.add(note_id)
                self.filesystem_writes += 1
            sequence += 1
            self.stop_event.wait(self.args.operation_interval)

    def search_worker(self) -> None:
        while not self.stop_event.is_set():
            result = self.retry(lambda: self.owner.search("SOAK-STABLE"), label="search")
            if not isinstance(result.get("items"), list):
                raise SoakInvariantError("search-response-malformed")
            with self.lock:
                self.searches += 1
            self.stop_event.wait(self.args.operation_interval / 2)

    def inbox_worker(self) -> None:
        while not self.stop_event.is_set():
            inbox = self.retry(lambda: self.reader.inbox("soak-reader", limit=100), label="inbox-read")
            cursor = inbox.get("cursor")
            if cursor:
                self.retry(lambda: self.reader.ack_inbox("soak-reader", cursor), label="inbox-ack")
                with self.lock:
                    self.inbox_acks += 1
            self.stop_event.wait(self.args.operation_interval / 2)

    def artifact_worker(self) -> None:
        sequence = 0
        while not self.stop_event.is_set():
            data = f"durable artifact {sequence}".encode("ascii")
            manifest = self.retry(lambda: raw_artifact(
                self.runtime.base, "soak-owner", data,
                filename=f"soak-{sequence}.txt", media_type="text/plain", visibility="shared",
            ), label="artifact-upload")
            with self.lock:
                self.artifact_ids.add(manifest["artifactId"])
            sequence += 1
            self.stop_event.wait(self.args.artifact_interval)

    def run_worker(self, label: str, worker: Callable[[], None]) -> None:
        try:
            worker()
        except SoakStopping:
            return
        except Exception as exc:
            self.failures.put(f"worker:{label}:{self.failure_code(exc)}")
            self.stop_event.set()

    def assert_reader_denied(self) -> None:
        try:
            self.reader.read_note(self.private_note_id)
        except HubClientError as exc:
            if exc.status != 404:
                raise
        else:
            raise RuntimeError("private note was readable by an unauthorized principal")
        with self.lock:
            self.authorization_checks += 1

    def sample(self) -> None:
        process = self.runtime.process
        if process is None or process.poll() is not None:
            return
        operation = lambda: self.admin.request("GET", "/v1/operations/diagnostics")
        diagnostics = operation() if self.stop_event.is_set() else self.retry(operation, label="diagnostics")
        with self.lock:
            self.rss_samples.append(rss_bytes(process.pid))
            self.state_samples.append(int(diagnostics["resources"]["stateBytes"]))

    def restart(self, *, hard: bool) -> None:
        self.runtime.stop(hard=hard)
        self.runtime.start()
        with self.lock:
            self.restarts["controlledKill" if hard else "graceful"] += 1
        self.assert_reader_denied()

    def authoritative_ids(self) -> tuple[set[str], set[str], set[str]]:
        task_db = self.state / "tasks" / "hub-tasks.sqlite"
        connection = sqlite3.connect(task_db)
        try:
            task_ids = {str(row[0]) for row in connection.execute("SELECT task_id FROM tasks")}
        finally:
            connection.close()
        note_ids = set()
        for path in (self.state / "memory" / "notes").rglob("*.md"):
            try:
                note_ids.add(str(parse_note(path.read_bytes())["id"]))
            except Exception:
                continue
        artifact_ids = {path.stem for path in (self.state / "artifacts" / "manifests").glob("*.json")}
        return task_ids, note_ids, artifact_ids

    def delivery_audit(self) -> tuple[set[str], set[str], int]:
        ops_db = self.state / "memory" / "ops.sqlite"
        connection = sqlite3.connect(ops_db)
        try:
            rows = connection.execute(
                "SELECT note_id,sequence FROM deliveries WHERE recipient=?",
                ("agent.beta",),
            ).fetchall()
            cursor = connection.execute(
                "SELECT acked_sequence FROM consumer_cursors WHERE principal=? AND consumer_id=?",
                ("agent.beta", "soak-reader"),
            ).fetchone()
        finally:
            connection.close()
        acknowledged = int(cursor[0]) if cursor else 0
        actual = {str(row[0]) for row in rows}
        unacknowledged = {
            str(row[0]) for row in rows
            if str(row[0]) in self.delivery_note_ids and int(row[1]) > acknowledged
        }
        return actual, unacknowledged, acknowledged

    def offline_queue_diagnostics(self) -> dict[str, Any]:
        def count(database: Path, query: str) -> int:
            connection = sqlite3.connect(database, timeout=10)
            try:
                row = connection.execute(query).fetchone()
                return int(row[0]) if row else 0
            finally:
                connection.close()

        return {
            "stores": {
                "memory": {
                    "pendingJobs": count(
                        self.state / "memory" / "ops.sqlite",
                        "SELECT COUNT(*) FROM jobs WHERE state NOT IN ('done', 'completed')",
                    ),
                    "activeQuarantine": count(
                        self.state / "memory" / "ops.sqlite",
                        "SELECT COUNT(*) FROM quarantine WHERE state='active'",
                    ),
                },
                "tasks": {
                    "pendingTerminalOutbox": count(
                        self.state / "tasks" / "hub-tasks.sqlite",
                        "SELECT COUNT(*) FROM terminal_outbox WHERE acknowledged_at IS NULL",
                    ),
                },
            },
        }

    def drain_deliveries(self, *, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            inbox = self.reader.inbox("soak-reader", limit=100)
            self.reader.ack_inbox("soak-reader", inbox["cursor"])
            with self.lock:
                self.inbox_acks += 1
                expected = set(self.delivery_note_ids)
            actual, unacknowledged, _ = self.delivery_audit()
            if expected <= actual and not unacknowledged:
                return
            time.sleep(0.2)

    def run(self) -> dict[str, Any]:
        self.prepare()
        started_at = utc_now()
        started = time.monotonic()
        next_restart = started + self.args.restart_interval
        next_sample = started
        hard = False
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = [
                executor.submit(self.run_worker, label, worker)
                for label, worker in (
                    ("task", self.task_worker),
                    ("note", self.note_worker),
                    ("filesystem", self.filesystem_worker),
                    ("search", self.search_worker),
                    ("inbox", self.inbox_worker),
                    ("artifact", self.artifact_worker),
                )
            ]
            while not self.stop_event.is_set() and time.monotonic() - started < self.args.duration_seconds:
                now = time.monotonic()
                if now >= next_sample:
                    try:
                        self.sample()
                    except Exception as exc:
                        self.failures.put(f"sample:{self.failure_code(exc)}")
                        self.stop_event.set()
                        break
                    next_sample = now + self.args.sample_interval
                if now >= next_restart:
                    try:
                        self.restart(hard=hard)
                    except Exception as exc:
                        self.failures.put(f"restart:{self.failure_code(exc)}")
                        self.stop_event.set()
                        break
                    hard = not hard
                    next_restart = now + self.args.restart_interval
                time.sleep(0.1)
            self.stop_event.set()
            for future in futures:
                future.result(timeout=40)

        observed = time.monotonic() - started
        diagnostics = None
        try:
            if self.runtime.process is None or self.runtime.process.poll() is not None:
                self.runtime.start()
            self.assert_reader_denied()
            self.drain_deliveries()
            diagnostics = self.admin.request("GET", "/v1/operations/diagnostics")
        except Exception as exc:
            self.failures.put(f"finalize:{self.failure_code(exc)}")
        finally:
            try:
                self.runtime.stop(hard=False)
            except Exception as exc:
                self.failures.put(f"shutdown:{self.failure_code(exc)}")
        if diagnostics is None:
            diagnostics = self.offline_queue_diagnostics()
        actual_tasks, actual_notes, actual_artifacts = self.authoritative_ids()
        actual_deliveries, unacknowledged_deliveries, _ = self.delivery_audit()
        lost_tasks = self.task_ids - actual_tasks
        lost_notes = self.note_ids - actual_notes
        lost_artifacts = self.artifact_ids - actual_artifacts
        lost_deliveries = self.delivery_note_ids - actual_deliveries
        rss = [value for value in self.rss_samples if value > 0]
        state_samples = list(self.state_samples)
        failures = list(self.failures.queue)
        resource_ok = bool(rss) and max(rss) <= self.args.max_rss_bytes
        if len(rss) > 1:
            resource_ok = resource_ok and rss[-1] - rss[0] <= self.args.max_rss_growth_bytes
        resource_ok = resource_ok and bool(state_samples) and max(state_samples) <= self.args.max_state_bytes
        queue_ok = (
            diagnostics["stores"]["memory"]["pendingJobs"] == 0
            and diagnostics["stores"]["memory"]["activeQuarantine"] == 0
            and diagnostics["stores"]["tasks"]["pendingTerminalOutbox"] <= self.args.max_pending_outbox
        )
        duration_ok = observed >= self.args.duration_seconds
        passed = not any((
            failures, lost_tasks, lost_notes, lost_artifacts,
            lost_deliveries, unacknowledged_deliveries,
        )) and resource_ok and queue_ok and duration_ok
        evidence = {
            "schema": EVIDENCE_SCHEMA,
            "startedAt": started_at,
            "completedAt": utc_now(),
            "expectedDurationSeconds": self.args.duration_seconds,
            "observedDurationSeconds": round(observed, 3),
            "platform": platform.system().lower(),
            "python": platform.python_version(),
            "commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True,
            ).stdout.strip(),
            "configuration": {
                "operationIntervalSeconds": self.args.operation_interval,
                "artifactIntervalSeconds": self.args.artifact_interval,
                "restartIntervalSeconds": self.args.restart_interval,
                "sampleIntervalSeconds": self.args.sample_interval,
            },
            "workload": {
                "tasks": len(self.task_ids), "notes": len(self.note_ids), "artifacts": len(self.artifact_ids),
                "filesystemWrites": self.filesystem_writes,
                "searches": self.searches, "inboxAcknowledgements": self.inbox_acks,
                "authorizationChecks": self.authorization_checks, "connectionRetries": self.retries,
                "expectedDeliveries": len(self.delivery_note_ids),
            },
            "restarts": self.restarts,
            "audit": {
                "lostTasks": len(lost_tasks), "lostNotes": len(lost_notes), "lostArtifacts": len(lost_artifacts),
                "lostDeliveries": len(lost_deliveries),
                "unacknowledgedDeliveries": len(unacknowledged_deliveries),
                "activeQuarantine": diagnostics["stores"]["memory"]["activeQuarantine"],
                "pendingJobs": diagnostics["stores"]["memory"]["pendingJobs"],
                "pendingTerminalOutbox": diagnostics["stores"]["tasks"]["pendingTerminalOutbox"],
                "privateAuthorizationLeaks": 0,
                "pdfDerivationSearchVerified": bool(self.derived_note_id),
            },
            "resources": {
                "samples": len(rss),
                "rssFirstBytes": rss[0] if rss else 0,
                "rssLastBytes": rss[-1] if rss else 0,
                "rssMaxBytes": max(rss, default=0),
                "stateFirstBytes": state_samples[0] if state_samples else 0,
                "stateLastBytes": state_samples[-1] if state_samples else 0,
                "stateMaxBytes": max(state_samples, default=0),
                "bounds": {
                    "maxRssBytes": self.args.max_rss_bytes,
                    "maxRssGrowthBytes": self.args.max_rss_growth_bytes,
                    "maxStateBytes": self.args.max_state_bytes,
                },
            },
            "failures": failures,
            "passed": passed,
        }
        return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Nonexistent private working directory")
    parser.add_argument("--evidence", required=True, help="Sanitized JSON evidence output")
    parser.add_argument("--duration-seconds", type=float, default=86_400)
    parser.add_argument("--operation-interval", type=float, default=5)
    parser.add_argument("--artifact-interval", type=float, default=60)
    parser.add_argument("--restart-interval", type=float, default=1_800)
    parser.add_argument("--sample-interval", type=float, default=60)
    parser.add_argument("--max-rss-bytes", type=int, default=536_870_912)
    parser.add_argument("--max-rss-growth-bytes", type=int, default=134_217_728)
    parser.add_argument("--max-state-bytes", type=int, default=2_147_483_648)
    parser.add_argument("--max-pending-outbox", type=int, default=0)
    args = parser.parse_args()
    for name in ("duration_seconds", "operation_interval", "artifact_interval", "restart_interval", "sample_interval"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main() -> int:
    args = parse_args()
    evidence_path = Path(args.evidence).resolve()
    if evidence_path.exists():
        raise SystemExit("evidence destination already exists")
    soak = Soak(args)
    try:
        evidence = soak.run()
    except Exception as exc:
        try:
            soak.runtime.stop(hard=True)
        finally:
            evidence = {
                "schema": EVIDENCE_SCHEMA,
                "completedAt": utc_now(),
                "passed": False,
                "failures": [soak.failure_code(exc)],
            }
    atomic_json(evidence_path, evidence)
    print(json.dumps({"schema": EVIDENCE_SCHEMA, "passed": evidence["passed"]}))
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
