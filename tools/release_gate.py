"""Build, clean-install, upgrade, roll back, and re-upgrade release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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
        run([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home),
        ], cwd=ROOT)

        install(python, current_wheel)
        current_version = version(python)
        validation = parse_json([str(python), "-m", "a2a_superhub", "skill", "validate"], cwd=ROOT)
        upgraded_skill = parse_json([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home), "--force",
        ], cwd=ROOT)
        diagnostics = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(state), "operations", "diagnostics",
        ], cwd=ROOT)
        backup = workspace / "backup.zip"
        parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(state), "operations", "backup", "create",
            "--destination", str(backup),
        ], cwd=ROOT)
        restored = workspace / "restored"
        restore = parse_json([
            str(python), "-m", "a2a_superhub", "operations", "backup", "restore",
            "--archive", str(backup), "--target-state", str(restored),
        ], cwd=ROOT)

        install(python, previous_wheel)
        rolled_back_version = version(python)
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
        forward_diagnostics = parse_json([
            str(python), "-m", "a2a_superhub", "--state", str(restored), "operations", "diagnostics",
        ], cwd=ROOT)
        final_skill = parse_json([
            str(python), "-m", "a2a_superhub", "skill", "install", "--target", "codex",
            "--target-root", str(codex_home), "--force",
        ], cwd=ROOT)
        forward_skill_baseline = installed_skill_baseline(codex_home)

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
            "upgradeFromPrevious": current_version != previous_version,
            "authoritativeBackupRestore": restore.get("integrity") == "verified",
            "rollbackVersion": previous_version == rolled_back_version,
            "rollbackReadsTask": task_id in task_ids,
            "rollbackReadsArtifact": artifact_id in artifact_ids,
            "forwardUpgrade": current_version == forward_version,
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
