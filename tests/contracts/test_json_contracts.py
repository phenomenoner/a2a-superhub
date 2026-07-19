from __future__ import annotations

import ipaddress
import json
import re
import unittest
import unicodedata
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
