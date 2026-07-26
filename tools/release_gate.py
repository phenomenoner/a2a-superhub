"""Build, clean-install, upgrade, roll back, and re-upgrade release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import venv
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "a2a-superhub.release-gate.v1"
MEMORY_OPS_SCHEMA_V3 = 3
MEMORY_OPS_SCHEMA_V4 = 4
MEMORY_LEGACY_TABLES = frozenset({
    "deliveries", "consumer_cursors", "issued_cursors", "receipts",
})
MEMORY_V4_TABLES = frozenset({
    "logical_deliveries",
    "logical_delivery_reasons",
    "delivery_aliases",
    "delivery_sequence_state",
    "delivery_route_snapshots",
    "logical_ack_receipts",
    "issued_cursor_items",
})
MEMORY_COUNT_TABLES = tuple(sorted(MEMORY_LEGACY_TABLES | MEMORY_V4_TABLES))

_CREATE_V3_MEMORY_FIXTURE = r"""
import json
import sys
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.memory import MemoryService

state = Path(sys.argv[1])
writer = Principal(
    "agent.alpha", "agent", "tok_release_writer",
    frozenset({"memory.read", "memory.write", "memory.share"}),
)
receiver = Principal(
    "agent.beta", "agent", "tok_release_receiver",
    frozenset({"memory.read"}),
)
service = MemoryService(state, enable_delivery=True)
created = service.create_note(
    {
        "type": "handoff",
        "title": "release gate memory schema fixture",
        "visibility": "direct:agent.beta",
        "about": ["agent.beta"],
        "body": "release gate memory schema fixture",
    },
    writer,
    idempotency_key="release-gate-memory-v3",
)
first = service.fetch_inbox(receiver, "release.gate", limit=1)
service.acknowledge_inbox(receiver, "release.gate", first["cursor"])
pending = service.fetch_inbox(receiver, "release.gate", limit=100)
print(json.dumps({
    "noteId": created.note["id"],
    "legacyDeliveries": len(service.list_deliveries()),
    "firstItems": len(first["items"]),
    "pendingItems": len(pending["items"]),
}))
"""

_ACTIVATE_MEMORY_SCHEMA = r"""
import json
import sys
from pathlib import Path

from a2a_superhub.memory import MemoryService

service = MemoryService(Path(sys.argv[1]), enable_delivery=True)
service.init()
print(json.dumps({"initialized": True}))
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(command: list[str], *, cwd: Path, capture: bool = False) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    return subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        encoding="utf-8",
        capture_output=capture,
        env=environment,
    )


def parse_json(command: list[str], *, cwd: Path) -> dict[str, Any]:
    return json.loads(run(command, cwd=cwd, capture=True).stdout)


def python_in(venv_root: Path) -> Path:
    return venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def build(builder: Path, source: Path, destination: Path) -> tuple[Path, Path]:
    destination.mkdir(parents=True)
    run([str(builder), "-m", "build", "--outdir", str(destination), str(source)], cwd=destination.parent)
    wheels = list(destination.glob("*.whl"))
    sdists = list(destination.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("build must produce exactly one wheel and one source archive")
    return wheels[0], sdists[0]


def install(python: Path, package: Path) -> None:
    run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--force-reinstall", str(package)], cwd=ROOT)


def version(python: Path) -> str:
    return run(
        [str(python), "-c", "import a2a_superhub; print(a2a_superhub.__version__)"],
        cwd=ROOT,
        capture=True,
    ).stdout.strip()


def clean_install_probe(workspace: Path, package: Path, *, label: str) -> dict[str, Any]:
    environment = workspace / f"{label}-venv"
    venv.EnvBuilder(with_pip=True, clear=True).create(environment)
    python = python_in(environment)
    install(python, package)
    state = workspace / f"{label}-state"
    codex_home = workspace / f"{label}-codex-home"
    run([str(python), "-m", "a2a_superhub", "--state", str(state), "init"], cwd=ROOT)
    diagnostics = parse_json([
        str(python), "-m", "a2a_superhub", "--state", str(state),
        "operations", "diagnostics",
    ], cwd=ROOT)
    validation = parse_json([str(python), "-m", "a2a_superhub", "skill", "validate"], cwd=ROOT)
    skill = parse_json([
        str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
        "--target-root", str(codex_home),
    ], cwd=ROOT)
    return {
        "version": version(python),
        "diagnosticsPayloadFree": diagnostics.get("payloadFree") is True,
        "skillValid": validation.get("valid") is True,
        "skillInstalled": skill.get("installed") is True,
        "skillBackupAbsent": not skill.get("backup"),
        "skillBaseline": installed_skill_baseline(codex_home),
    }


def installed_skill_baseline(codex_home: Path) -> str:
    compatibility = (
        codex_home / "skills" / "operate-a2a-superhub" / "references" / "compatibility.json"
    )
    value = json.loads(compatibility.read_text(encoding="utf-8"))
    return str(value.get("productBaseline") or "")


def create_authoritative_backup(python: Path, state: Path, destination: Path) -> dict[str, Any]:
    return parse_json([
        str(python), "-m", "a2a_superhub", "--state", str(state),
        "operations", "backup", "create", "--destination", str(destination),
    ], cwd=ROOT)


def restore_authoritative_backup(python: Path, archive: Path, target_state: Path) -> dict[str, Any]:
    return parse_json([
        str(python), "-m", "a2a_superhub", "operations", "backup", "restore",
        "--archive", str(archive), "--target-state", str(target_state),
    ], cwd=ROOT)


def create_v3_memory_fixture(python: Path, state: Path) -> dict[str, Any]:
    fixture = parse_json(
        [str(python), "-c", _CREATE_V3_MEMORY_FIXTURE, str(state)],
        cwd=ROOT,
    )
    expected = {"legacyDeliveries": 3, "firstItems": 1, "pendingItems": 2}
    if any(fixture.get(key) != value for key, value in expected.items()):
        raise RuntimeError("previous package did not create the expected v3 memory fixture")
    note_id = fixture.get("noteId")
    if not isinstance(note_id, str) or not note_id.startswith("mem_"):
        raise RuntimeError("previous package returned an invalid memory fixture identity")
    return fixture


def activate_memory_schema(python: Path, state: Path) -> None:
    result = parse_json(
        [str(python), "-c", _ACTIVATE_MEMORY_SCHEMA, str(state)],
        cwd=ROOT,
    )
    if result.get("initialized") is not True:
        raise RuntimeError("candidate memory schema initialization did not complete")


def read_memory_note(python: Path, state: Path, note_id: str) -> dict[str, Any]:
    return parse_json([
        str(python), "-m", "a2a_superhub", "--state", str(state),
        "memory", "note", "read", note_id,
    ], cwd=ROOT)


def memory_ops_inventory(state: Path) -> dict[str, Any]:
    database = state / "memory" / "ops.sqlite"
    if not database.is_file():
        raise RuntimeError("memory ops database is missing")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in MEMORY_COUNT_TABLES
            if table in tables
        }
    finally:
        connection.close()
    return {
        "userVersion": user_version,
        "tables": sorted(tables),
        "counts": counts,
    }


def require_memory_ops(state: Path, expected_version: int) -> dict[str, Any]:
    inventory = memory_ops_inventory(state)
    if inventory["userVersion"] != expected_version:
        raise RuntimeError(
            f"memory ops schema is v{inventory['userVersion']}, expected v{expected_version}"
        )
    tables = set(inventory["tables"])
    required = set(MEMORY_LEGACY_TABLES)
    if expected_version == MEMORY_OPS_SCHEMA_V4:
        required.update(MEMORY_V4_TABLES)
    missing = sorted(required - tables)
    if missing:
        raise RuntimeError("memory ops schema is missing required tables: " + ", ".join(missing))
    if expected_version == MEMORY_OPS_SCHEMA_V3:
        unexpected = sorted(MEMORY_V4_TABLES & tables)
        if unexpected:
            raise RuntimeError(
                "v3 rollback state contains candidate-only memory tables: "
                + ", ".join(unexpected)
            )
    return inventory


def validate_memory_schema_drill(
    pre_upgrade: dict[str, Any],
    candidate: dict[str, Any],
    rollback: dict[str, Any],
    forward: dict[str, Any],
) -> bool:
    versions = (
        pre_upgrade["userVersion"],
        candidate["userVersion"],
        rollback["userVersion"],
        forward["userVersion"],
    )
    if versions != (
        MEMORY_OPS_SCHEMA_V3,
        MEMORY_OPS_SCHEMA_V4,
        MEMORY_OPS_SCHEMA_V3,
        MEMORY_OPS_SCHEMA_V4,
    ):
        raise RuntimeError("memory schema drill did not follow v3 -> v4 -> restored v3 -> v4")

    pre_counts = pre_upgrade["counts"]
    for stage_name, inventory in (
        ("candidate", candidate),
        ("rollback", rollback),
        ("forward", forward),
    ):
        for table in MEMORY_LEGACY_TABLES:
            if inventory["counts"].get(table) != pre_counts.get(table):
                raise RuntimeError(
                    f"{stage_name} memory state changed legacy {table} cardinality"
                )

    legacy_deliveries = pre_counts.get("deliveries", 0)
    if legacy_deliveries != 3:
        raise RuntimeError("memory schema drill requires the three-reason v3 fixture")
    for stage_name, inventory in (("candidate", candidate), ("forward", forward)):
        counts = inventory["counts"]
        if counts.get("logical_deliveries") != 1:
            raise RuntimeError(f"{stage_name} did not produce one logical delivery")
        if counts.get("logical_delivery_reasons") != legacy_deliveries:
            raise RuntimeError(f"{stage_name} did not preserve the complete reason set")
        if counts.get("delivery_aliases") != legacy_deliveries:
            raise RuntimeError(f"{stage_name} did not alias every legacy delivery")
    for table in MEMORY_V4_TABLES:
        if candidate["counts"].get(table) != forward["counts"].get(table):
            raise RuntimeError(f"forward migration changed candidate {table} cardinality")

    if MEMORY_V4_TABLES & set(rollback["tables"]):
        raise RuntimeError("rollback state contains v4 memory schema")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-ref", required=True, help="Git ref for the supported previous package")
    parser.add_argument("--evidence", required=True, help="Nonexistent sanitized JSON output")
    args = parser.parse_args()
    evidence_path = Path(args.evidence).resolve()
    if evidence_path.exists():
        raise SystemExit("evidence destination already exists")

    started = now()
    with tempfile.TemporaryDirectory(prefix="a2a-superhub-release-gate-") as temporary:
        workspace = Path(temporary)
        builder_environment = workspace / "builder-venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(builder_environment)
        builder = python_in(builder_environment)
        run(
            [str(builder), "-m", "pip", "install", "--disable-pip-version-check", "build==1.3.0"],
            cwd=workspace,
        )
        current_wheel, current_sdist = build(builder, ROOT, workspace / "current-dist")
        clean_wheel = clean_install_probe(workspace, current_wheel, label="current-wheel-clean")
        clean_sdist = clean_install_probe(workspace, current_sdist, label="current-sdist-clean")

        previous_zip = workspace / "previous.zip"
        run(["git", "archive", "--format=zip", "-o", str(previous_zip), args.previous_ref], cwd=ROOT)
        previous_source = workspace / "previous-source"
        previous_source.mkdir()
        with zipfile.ZipFile(previous_zip) as archive:
            archive.extractall(previous_source)
        previous_wheel, previous_sdist = build(builder, previous_source, workspace / "previous-dist")

        environment = workspace / "venv"
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python = python_in(environment)
        run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "PyYAML==6.0.3", "watchdog==6.0.0"],
            cwd=ROOT,
        )
        state = workspace / "state"
        artifact_file = workspace / "artifact.bin"
        artifact_file.write_bytes(b"release gate artifact")
        codex_home = workspace / "codex-home"

        install(python, previous_wheel)
        previous_version = version(python)
        run([str(python), "-m", "a2a_superhub", "--state", str(state), "init"], cwd=ROOT)
        created_task = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(state), "task", "create",
            "--from-agent", "agent.alpha", "--to-agent", "agent.beta",
            "--summary", "release gate", "--idempotency-key", "release-gate-task",
        ], cwd=ROOT)
        created_artifact = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(state), "artifact", "put",
            "--file", str(artifact_file), "--created-by", "agent.alpha", "--visibility", "shared",
        ], cwd=ROOT)
        memory_fixture = create_v3_memory_fixture(python, state)
        pre_upgrade_memory = require_memory_ops(state, MEMORY_OPS_SCHEMA_V3)
        run([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home),
        ], cwd=ROOT)

        install(python, current_wheel)
        current_version = version(python)
        backup = workspace / "pre-upgrade-v3-backup.zip"
        backup_result = create_authoritative_backup(python, state, backup)

        activate_memory_schema(python, state)
        candidate_memory = require_memory_ops(state, MEMORY_OPS_SCHEMA_V4)
        candidate_note = read_memory_note(python, state, memory_fixture["noteId"])
        validation = parse_json([str(python), "-m", "a2a_superhub", "skill", "validate"], cwd=ROOT)
        upgraded_skill = parse_json([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home), "--force",
        ], cwd=ROOT)
        diagnostics = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(state), "operations", "diagnostics",
        ], cwd=ROOT)
        restored = workspace / "rollback-state"
        restore = restore_authoritative_backup(python, backup, restored)
        restored_memory = require_memory_ops(restored, MEMORY_OPS_SCHEMA_V3)

        install(python, previous_wheel)
        rolled_back_version = version(python)
        rollback_memory_stats = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(restored), "memory", "stats",
        ], cwd=ROOT)
        rollback_note = read_memory_note(python, restored, memory_fixture["noteId"])
        rollback_memory = require_memory_ops(restored, MEMORY_OPS_SCHEMA_V3)
        rollback_tasks = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(restored), "task", "list",
        ], cwd=ROOT)
        rollback_artifacts = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(restored), "artifact", "list",
        ], cwd=ROOT)
        rollback_skill = parse_json([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home), "--force",
        ], cwd=ROOT)
        rollback_skill_baseline = installed_skill_baseline(codex_home)

        install(python, current_wheel)
        forward_version = version(python)
        activate_memory_schema(python, restored)
        forward_memory = require_memory_ops(restored, MEMORY_OPS_SCHEMA_V4)
        forward_note = read_memory_note(python, restored, memory_fixture["noteId"])
        forward_diagnostics = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(restored), "operations", "diagnostics",
        ], cwd=ROOT)
        final_skill = parse_json([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home), "--force",
        ], cwd=ROOT)
        forward_skill_baseline = installed_skill_baseline(codex_home)
        memory_schema_drill = validate_memory_schema_drill(
            pre_upgrade_memory,
            candidate_memory,
            rollback_memory,
            forward_memory,
        )

        task_id = created_task["taskId"] if "taskId" in created_task else created_task["task"]["taskId"]
        artifact_id = created_artifact["artifactId"]
        task_ids = {item["taskId"] for item in rollback_tasks["tasks"]}
        artifact_ids = {item["artifactId"] for item in rollback_artifacts["artifacts"]}
        checks = {
            "currentWheelCleanInstall": (
                clean_wheel["version"] == current_version
                and clean_wheel["diagnosticsPayloadFree"]
            ),
            "currentSdistCleanInstall": (
                clean_sdist["version"] == current_version
                and clean_sdist["diagnosticsPayloadFree"]
            ),
            "currentSkillCleanInstall": all((
                clean_wheel["skillValid"], clean_wheel["skillInstalled"], clean_wheel["skillBackupAbsent"],
                clean_wheel["skillBaseline"] == current_version,
                clean_sdist["skillValid"], clean_sdist["skillInstalled"], clean_sdist["skillBackupAbsent"],
                clean_sdist["skillBaseline"] == current_version,
            )),
            "upgradeFromPrevious": all((
                current_version != previous_version,
                memory_schema_drill,
                candidate_note.get("id") == memory_fixture["noteId"],
            )),
            "authoritativeBackupRestore": all((
                backup_result.get("archiveSha256") == sha256(backup),
                restore.get("integrity") == "verified",
                restored_memory["counts"] == pre_upgrade_memory["counts"],
            )),
            "rollbackVersion": all((
                previous_version == rolled_back_version,
                rollback_memory_stats.get("deliveryBacklog") == 3,
                rollback_note.get("id") == memory_fixture["noteId"],
                rollback_memory["userVersion"] == MEMORY_OPS_SCHEMA_V3,
            )),
            "rollbackReadsTask": task_id in task_ids,
            "rollbackReadsArtifact": artifact_id in artifact_ids,
            "forwardUpgrade": all((
                current_version == forward_version,
                forward_note.get("id") == memory_fixture["noteId"],
                forward_memory["userVersion"] == MEMORY_OPS_SCHEMA_V4,
                memory_schema_drill,
            )),
            "skillUpgradeValidated": validation.get("valid") is True,
            "skillUpgradeBackupCreated": bool(upgraded_skill.get("backup")),
            "skillRollbackAndForward": all((
                rollback_skill.get("installed") is True,
                rollback_skill_baseline == previous_version,
                final_skill.get("installed") is True,
                forward_skill_baseline == current_version,
            )),
            "diagnosticsPayloadFree": diagnostics["payloadFree"] is True and forward_diagnostics["payloadFree"] is True,
        }
        passed = all(checks.values())
        evidence = {
            "schema": SCHEMA,
            "startedAt": started,
            "completedAt": now(),
            "currentRef": run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture=True).stdout.strip(),
            "previousRef": run(["git", "rev-parse", args.previous_ref], cwd=ROOT, capture=True).stdout.strip(),
            "versions": {
                "previous": previous_version,
                "currentWheelClean": clean_wheel["version"],
                "currentSdistClean": clean_sdist["version"],
                "currentWheelSkill": clean_wheel["skillBaseline"],
                "currentSdistSkill": clean_sdist["skillBaseline"],
                "current": current_version,
                "rollback": rolled_back_version,
                "rollbackSkill": rollback_skill_baseline,
                "forward": forward_version,
                "forwardSkill": forward_skill_baseline,
            },
            "artifacts": {
                "previousWheelSha256": sha256(previous_wheel),
                "previousSdistSha256": sha256(previous_sdist),
                "currentWheelSha256": sha256(current_wheel),
                "currentSdistSha256": sha256(current_sdist),
            },
            "checks": checks,
            "passed": passed,
        }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "passed": passed}))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
