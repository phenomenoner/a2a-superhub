from __future__ import annotations

import ipaddress
import json
import re
import tempfile
import unittest
import unicodedata
import zipfile
from pathlib import Path

from a2a_superhub.auth import Principal
from a2a_superhub.operations import BackupManager, OperationsDiagnostics
from a2a_superhub.store import HubStore

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # Core-only installs intentionally have no contract dependencies.
    Draft202012Validator = None
    FormatChecker = None


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "contracts" / "fixtures"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def safe_relative_path(value: str) -> bool:
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        return False
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for segment in normalized.split("/"):
        if not segment or segment in {".", ".."} or segment.endswith((".", " ")):
            return False
        if segment.split(".", 1)[0].upper() in reserved:
            return False
    return True


def path_key(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


@unittest.skipIf(Draft202012Validator is None, "install the contracts extra for JSON Schema validation")
class JsonSchemaContractTests(unittest.TestCase):
    def validator(self, name: str) -> Draft202012Validator:
        schema = load_json(ROOT / "schemas" / name)
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def test_memory_note_valid_and_invalid_fixtures(self) -> None:
        validator = self.validator("memory-note-v1.schema.json")
        for path in sorted((FIXTURES / "memory").glob("valid-*.json")):
            self.assertEqual([], list(validator.iter_errors(load_json(path))), path.name)
        for path in sorted((FIXTURES / "memory").glob("invalid-*.json")):
            self.assertTrue(list(validator.iter_errors(load_json(path))), path.name)

    def test_principal_and_startup_contract(self) -> None:
        validator = self.validator("principal-v1.schema.json")
        cases = load_json(FIXTURES / "principal" / "cases.json")
        for instance in cases["valid"]:
            self.assertEqual([], list(validator.iter_errors(instance)), instance)
        for instance in cases["invalid"]:
            self.assertTrue(list(validator.iter_errors(instance)), instance)
        for case in cases["startup"]:
            is_loopback = ipaddress.ip_address(case["bind"]).is_loopback
            actual = "allow-authenticated" if case["authConfigured"] else ("allow-local-operator" if is_loopback else "deny")
            self.assertEqual(case["expected"], actual, case)

    def test_api_examples_and_negative_cases(self) -> None:
        schema = load_json(ROOT / "schemas" / "memory-api-v1.schema.json")
        Draft202012Validator.check_schema(schema)
        cases = load_json(FIXTURES / "api" / "cases.json")
        for case in cases["valid"]:
            selected = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{case['schemaDef']}"}
            validator = Draft202012Validator(selected, format_checker=FormatChecker())
            self.assertEqual([], list(validator.iter_errors(case["instance"])), case)
        for case in cases["invalid"]:
            selected = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{case['schemaDef']}"}
            validator = Draft202012Validator(selected, format_checker=FormatChecker())
            self.assertTrue(list(validator.iter_errors(case["instance"])), case)

    def test_size_boundaries_are_utf8_aware(self) -> None:
        body_limit = load_json(ROOT / "schemas" / "agent-surface-v1.json")["limits"]["noteBodyUtf8Bytes"]
        self.assertEqual(body_limit, len(("a" * body_limit).encode("utf-8")))
        self.assertGreater(len(("台" * (body_limit // 3 + 1)).encode("utf-8")), body_limit)
        title_limit = load_json(ROOT / "schemas" / "agent-surface-v1.json")["limits"]["titleCodePoints"]
        self.assertEqual(title_limit, len("界" * title_limit))
        self.assertEqual(title_limit + 1, len("界" * (title_limit + 1)))

    def test_path_unicode_reserved_and_duplicate_fixtures(self) -> None:
        cases = load_json(FIXTURES / "paths" / "cases.json")
        for value in cases["valid"]:
            self.assertTrue(safe_relative_path(value), value)
        for case in cases["invalid"]:
            self.assertFalse(safe_relative_path(case["path"]), case)
        for case in cases["collisionSets"]:
            keys = {path_key(value) for value in case["paths"]}
            self.assertEqual(1, len(keys), case)
        for case in cases["duplicateIds"]:
            self.assertGreater(len(set(case["paths"])), 1)
            self.assertEqual("quarantine-both", case["expected"])

    def test_evidence_record_format(self) -> None:
        validator = self.validator("evidence-record-v1.schema.json")
        fixture = load_json(FIXTURES / "evidence" / "example.json")
        self.assertEqual([], list(validator.iter_errors(fixture)))

    def test_artifact_transport_and_derivation_contract(self) -> None:
        schema = load_json(ROOT / "schemas" / "artifact-api-v1.schema.json")
        Draft202012Validator.check_schema(schema)
        instance = {
            "contract": schema["contract"],
            "manifestSchema": schema["manifestSchema"],
            "transports": schema["transports"],
            "derivation": schema["derivation"],
            "manifest": {
                "schema": "a2a-superhub.artifact.v1",
                "artifactId": "art_" + "a" * 32,
                "sha256": "a" * 64,
                "sizeBytes": 3,
                "mediaType": "text/plain",
                "filename": "a.txt",
                "storageUri": "hub-cas://sha256/" + "a" * 64,
                "createdBy": "agent.alpha",
                "createdAt": "2026-07-20T00:00:00Z",
                "visibility": "private",
                "policy": {},
            },
        }
        self.assertEqual([], list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(instance)))

    def test_operational_backup_restore_and_diagnostics_contracts(self) -> None:
        schema = load_json(ROOT / "schemas" / "operations-v1.schema.json")
        Draft202012Validator.check_schema(schema)
        admin = Principal("local.operator", "operator", "tok_contract", frozenset({"hub.admin"}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            HubStore(state).init()
            archive_path = root / "backup.zip"
            BackupManager(state).create(archive_path)
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            restore = BackupManager.restore(archive_path, root / "restored")
            diagnostics = OperationsDiagnostics(root / "restored").collect(admin)
        for definition, instance in (
            ("backupManifest", manifest),
            ("restoreResult", restore),
            ("diagnostics", diagnostics),
            ("singleHubSoak", {
                "schema": "a2a-superhub.single-hub-soak.v1",
                "startedAt": "2026-07-20T00:00:00Z",
                "completedAt": "2026-07-21T00:00:00Z",
                "expectedDurationSeconds": 86400,
                "observedDurationSeconds": 86401,
                "platform": "windows",
                "python": "3.12.0",
                "commit": "a" * 40,
                "configuration": {
                    "operationIntervalSeconds": 120,
                    "artifactIntervalSeconds": 120,
                    "restartIntervalSeconds": 1800,
                    "sampleIntervalSeconds": 60,
                },
                "workload": {
                    "tasks": 1, "notes": 2, "artifacts": 1, "filesystemWrites": 1,
                    "searches": 1, "inboxAcknowledgements": 1, "authorizationChecks": 1,
                    "connectionRetries": 0, "expectedDeliveries": 2,
                },
                "restarts": {"graceful": 1, "controlledKill": 1},
                "audit": {
                    "lostTasks": 0, "lostNotes": 0, "lostArtifacts": 0,
                    "lostDeliveries": 0, "unacknowledgedDeliveries": 0,
                    "activeQuarantine": 0, "pendingJobs": 0, "pendingTerminalOutbox": 0,
                    "privateAuthorizationLeaks": 0, "pdfDerivationSearchVerified": True,
                },
                "resources": {},
                "failures": [], "passed": True,
            }),
            ("releaseGate", {
                "schema": "a2a-superhub.release-gate.v1",
                "startedAt": "2026-07-20T00:00:00Z",
                "completedAt": "2026-07-20T01:00:00Z",
                "currentRef": "a" * 40,
                "previousRef": "b" * 40,
                "versions": {
                    "previous": "0.1.0", "currentWheelClean": "0.2.0",
                    "currentSdistClean": "0.2.0", "currentWheelSkill": "0.2.0",
                    "currentSdistSkill": "0.2.0", "current": "0.2.0",
                    "rollback": "0.1.0", "rollbackSkill": "0.1.0",
                    "forward": "0.2.0", "forwardSkill": "0.2.0",
                },
                "artifacts": {
                    "previousWheelSha256": "a" * 64, "previousSdistSha256": "b" * 64,
                    "currentWheelSha256": "c" * 64, "currentSdistSha256": "d" * 64,
                },
                "checks": {
                    "currentWheelCleanInstall": True, "currentSdistCleanInstall": True,
                    "currentSkillCleanInstall": True, "upgradeFromPrevious": True,
                    "authoritativeBackupRestore": True, "rollbackVersion": True,
                    "rollbackReadsTask": True, "rollbackReadsArtifact": True,
                    "forwardUpgrade": True, "skillUpgradeValidated": True,
                    "skillUpgradeBackupCreated": True, "skillRollbackAndForward": True,
                    "diagnosticsPayloadFree": True,
                },
                "passed": True,
            }),
        ):
            selected = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
            self.assertEqual(
                [],
                list(Draft202012Validator(selected, format_checker=FormatChecker()).iter_errors(instance)),
                definition,
            )


if __name__ == "__main__":
    unittest.main()
