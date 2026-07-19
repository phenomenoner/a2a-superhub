from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any


SKILL_NAME = "operate-a2a-superhub"
OWNERSHIP_FILE = ".a2a-superhub-install.json"
SKILL_PAYLOAD = (
    "SKILL.md",
    "agents/openai.yaml",
    "references/capabilities-and-versions.md",
    "references/compatibility.json",
    "references/security-and-approval.md",
    "references/troubleshooting.md",
    "references/workflows.md",
    "scripts/doctor.py",
    "scripts/smoke.py",
)


class SkillInstallError(RuntimeError):
    pass


def skill_source_path() -> Path:
    repo_candidate = Path(__file__).resolve().parents[2] / "skills" / SKILL_NAME
    if (repo_candidate / "SKILL.md").is_file():
        return repo_candidate
    packaged = Path(__file__).resolve().parent / "_skill" / SKILL_NAME
    if (packaged / "SKILL.md").is_file():
        return packaged
    raise SkillInstallError("the packaged operate-a2a-superhub Skill is not discoverable")


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _skill_files(root: Path) -> list[Path]:
    return [root / relative for relative in SKILL_PAYLOAD]


def _contract_root() -> Path:
    repo = Path(__file__).resolve().parents[2]
    if (repo / "schemas" / "agent-surface-v1.json").is_file():
        return repo
    packaged = Path(__file__).resolve().parent / "_contracts"
    if (packaged / "schemas" / "agent-surface-v1.json").is_file():
        return packaged
    raise SkillInstallError("agent-facing contract bundle is not discoverable")


def contract_fingerprint(root: Path, relative_files: list[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(relative_files):
        try:
            value = json.loads((root / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillInstallError(f"invalid or missing agent contract: {relative}") from exc
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(canonical)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def validate_skill(root: str | Path | None = None) -> dict[str, Any]:
    path = Path(root) if root is not None else skill_source_path()
    required = [
        "SKILL.md",
        "agents/openai.yaml",
        "references/compatibility.json",
        "scripts/doctor.py",
        "scripts/smoke.py",
    ]
    missing = [relative for relative in required if not (path / relative).is_file()]
    errors: list[str] = []
    if missing:
        errors.append("missing required files: " + ", ".join(missing))
    skill_file = path / "SKILL.md"
    if skill_file.is_file():
        text = skill_file.read_text(encoding="utf-8")
        if not text.startswith("---\n") or text.count("---\n") < 2:
            errors.append("SKILL.md frontmatter is invalid")
        else:
            frontmatter = text.split("---\n", 2)[1]
            keys = {line.split(":", 1)[0] for line in frontmatter.splitlines() if ":" in line}
            if keys != {"name", "description"}:
                errors.append("SKILL.md frontmatter must contain only name and description")
    actual_files = {
        item.relative_to(path).as_posix()
        for item in path.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc" and item.name != OWNERSHIP_FILE
    }
    unexpected = sorted(actual_files - set(SKILL_PAYLOAD))
    if unexpected:
        errors.append("unexpected Skill payload files: " + ", ".join(unexpected))
    compatibility_file = path / "references" / "compatibility.json"
    if compatibility_file.is_file():
        try:
            compatibility = json.loads(compatibility_file.read_text(encoding="utf-8"))
            revision = compatibility.get("agentSurfaceRevision")
            if not isinstance(revision, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", revision):
                errors.append("compatibility fingerprint is invalid")
            else:
                contract_files = compatibility.get("contractFiles")
                if not isinstance(contract_files, list) or not all(isinstance(item, str) for item in contract_files):
                    errors.append("compatibility contractFiles is invalid")
                else:
                    try:
                        actual_revision = contract_fingerprint(_contract_root(), contract_files)
                    except SkillInstallError as exc:
                        errors.append(str(exc))
                    else:
                        if actual_revision != revision:
                            errors.append("compatibility fingerprint does not match canonical agent contracts")
        except (OSError, json.JSONDecodeError):
            errors.append("compatibility.json is invalid")
    return {"valid": not errors, "skill": SKILL_NAME, "path": str(path.resolve()), "errors": errors}


def resolve_codex_home(explicit: str | Path | None) -> Path:
    if explicit is None:
        raw = os.environ.get("CODEX_HOME")
        if raw:
            candidate = Path(raw)
        else:
            user_profile = os.environ.get("USERPROFILE")
            if not user_profile:
                raise SkillInstallError("CODEX_HOME is unset and USERPROFILE is unavailable")
            candidate = Path(user_profile) / ".codex"
    else:
        candidate = Path(explicit)
    if not candidate.is_absolute():
        raise SkillInstallError("Codex target root must be an absolute path")
    resolved = candidate.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise SkillInstallError("filesystem roots are not valid Codex targets")
    return resolved


def _target_path(target: str, target_root: str | Path | None) -> Path:
    if target != "codex":
        raise SkillInstallError("only the codex Skill target is supported")
    root = resolve_codex_home(target_root)
    skills_dir = (root / "skills").resolve(strict=False)
    if not skills_dir.is_relative_to(root):
        raise SkillInstallError("resolved Codex skills directory escaped the Codex target root")
    raw_destination = skills_dir / SKILL_NAME
    is_junction = getattr(raw_destination, "is_junction", lambda: False)
    if raw_destination.is_symlink() or is_junction():
        raise SkillInstallError("refusing Skill target that is a symlink or junction")
    destination = raw_destination.resolve(strict=False)
    if destination.parent != skills_dir or not destination.is_relative_to(root):
        raise SkillInstallError("resolved Skill target escaped the Codex skills directory")
    return destination


def install_skill(
    *,
    target: str,
    target_root: str | Path | None = None,
    force: bool = False,
) -> dict[str, Any]:
    source = skill_source_path().resolve()
    validation = validate_skill(source)
    if not validation["valid"]:
        raise SkillInstallError("source Skill validation failed: " + "; ".join(validation["errors"]))
    destination = _target_path(target, target_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if destination.exists():
        if not force:
            raise SkillInstallError(f"Skill already exists at {destination}; use --force for a recoverable replacement")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = destination.with_name(f"{destination.name}.backup-{stamp}")
        if backup.exists():
            raise SkillInstallError("backup collision; retry installation")
        shutil.move(str(destination), str(backup))
    destination.mkdir(parents=True)
    for relative in SKILL_PAYLOAD:
        source_file = source / relative
        destination_file = destination / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)
    files = {
        path.relative_to(destination).as_posix(): _hash_file(path)
        for path in _skill_files(destination)
    }
    try:
        product_version = version("a2a-superhub")
    except PackageNotFoundError:
        product_version = "source"
    manifest = {
        "schema": "a2a-superhub.skill-install.v1",
        "installer": "a2a-superhub",
        "skill": SKILL_NAME,
        "productVersion": product_version,
        "files": files,
    }
    (destination / OWNERSHIP_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"installed": True, "path": str(destination), "backup": str(backup) if backup else None, "files": len(files)}


def uninstall_skill(*, target: str, target_root: str | Path | None = None) -> dict[str, Any]:
    destination = _target_path(target, target_root)
    manifest_path = destination / OWNERSHIP_FILE
    if not manifest_path.is_file():
        raise SkillInstallError("refusing uninstall: installer ownership manifest is absent")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise SkillInstallError("refusing uninstall: ownership manifest is invalid") from None
    if manifest.get("installer") != "a2a-superhub" or manifest.get("skill") != SKILL_NAME:
        raise SkillInstallError("refusing uninstall: ownership manifest does not match this installer")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files or any(
        not isinstance(relative, str)
        or relative not in SKILL_PAYLOAD
        or not isinstance(expected_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
        for relative, expected_hash in files.items()
    ):
        raise SkillInstallError("refusing uninstall: ownership file list is invalid")
    removed_files: list[str] = []
    retained_files: list[str] = []
    removable: list[tuple[str, Path]] = []
    for relative, expected_hash in sorted(files.items()):
        candidate = (destination / relative).resolve(strict=False)
        if destination not in candidate.parents:
            raise SkillInstallError("refusing uninstall: ownership entry escapes the installed Skill")
        if candidate.is_file() and _hash_file(candidate) == expected_hash:
            removable.append((relative, candidate))
        elif candidate.exists():
            retained_files.append(relative)
    for relative, candidate in removable:
        candidate.unlink()
        removed_files.append(relative)
    manifest_path.unlink()
    for directory in sorted((path for path in destination.rglob("*") if path.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        destination.rmdir()
    except OSError:
        pass
    return {
        "removed": True,
        "path": str(destination),
        "removedFiles": removed_files,
        "retainedFiles": retained_files,
    }
