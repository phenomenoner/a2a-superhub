from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_gate", ROOT / "tools" / "release_gate.py")
assert SPEC and SPEC.loader
release_gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release_gate)


class ReleaseGateIsolationTests(unittest.TestCase):
    @staticmethod
    def _write_memory_ops_fixture(state: Path, *, version: int, logical: bool) -> None:
        database = state / "memory" / "ops.sqlite"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        try:
            for table, count in {
                "deliveries": 3,
                "consumer_cursors": 1,
                "issued_cursors": 3,
                "receipts": 7,
            }.items():
                connection.execute(f'CREATE TABLE "{table}"(value INTEGER)')
                connection.executemany(
                    f'INSERT INTO "{table}"(value) VALUES (?)',
                    [(index,) for index in range(count)],
                )
            if logical:
                for table, count in {
                    "logical_deliveries": 1,
                    "logical_delivery_reasons": 3,
                    "delivery_aliases": 3,
                    "delivery_sequence_state": 1,
                    "delivery_route_snapshots": 1,
                    "logical_ack_receipts": 0,
                    "issued_cursor_items": 0,
                }.items():
                    connection.execute(f'CREATE TABLE "{table}"(value INTEGER)')
                    connection.executemany(
                        f'INSERT INTO "{table}"(value) VALUES (?)',
                        [(index,) for index in range(count)],
                    )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        finally:
            connection.close()

    def test_subprocesses_ignore_ambient_python_import_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "environment.py"
            script.write_text(
                "import json, os; print(json.dumps({key: os.environ.get(key) for key in "
                "('PYTHONHOME', 'PYTHONPATH', 'PYTHONNOUSERSITE')}))\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"PYTHONHOME": "untrusted-home", "PYTHONPATH": "untrusted-source"},
            ):
                completed = release_gate.run(
                    [sys.executable, str(script)], cwd=Path(temporary), capture=True,
                )
            child = json.loads(completed.stdout)
            self.assertIsNone(child["PYTHONHOME"])
            self.assertIsNone(child["PYTHONPATH"])
            self.assertEqual("1", child["PYTHONNOUSERSITE"])

    def test_memory_schema_drill_requires_v3_v4_restored_v3_v4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            states = {
                "pre": (3, False),
                "candidate": (4, True),
                "rollback": (3, False),
                "forward": (4, True),
            }
            inventories = {}
            for name, (version, logical) in states.items():
                state = root / name
                self._write_memory_ops_fixture(state, version=version, logical=logical)
                inventories[name] = release_gate.require_memory_ops(state, version)

            self.assertTrue(release_gate.validate_memory_schema_drill(
                inventories["pre"],
                inventories["candidate"],
                inventories["rollback"],
                inventories["forward"],
            ))

            with self.assertRaisesRegex(RuntimeError, "v3 -> v4 -> restored v3 -> v4"):
                release_gate.validate_memory_schema_drill(
                    inventories["pre"],
                    inventories["candidate"],
                    inventories["candidate"],
                    inventories["forward"],
                )

    def test_pre_upgrade_backup_restores_v3_after_source_state_moves_to_v4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "source-state"
            self._write_memory_ops_fixture(state, version=3, logical=False)
            database = state / "memory" / "ops.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE rollback_sentinel(value TEXT NOT NULL)")
                connection.execute(
                    "INSERT INTO rollback_sentinel(value) VALUES ('pre-upgrade-v3')"
                )
                connection.commit()

            backup = root / "pre-upgrade-v3.zip"
            result = release_gate.create_authoritative_backup(
                Path(sys.executable), state, backup,
            )
            self.assertEqual(release_gate.sha256(backup), result["archiveSha256"])

            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE logical_deliveries(value INTEGER)")
                connection.execute("PRAGMA user_version=4")
                connection.commit()

            rollback_state = root / "rollback-state"
            restored = release_gate.restore_authoritative_backup(
                Path(sys.executable), backup, rollback_state,
            )
            self.assertEqual("verified", restored["integrity"])
            inventory = release_gate.require_memory_ops(rollback_state, 3)
            self.assertNotIn("logical_deliveries", inventory["tables"])
            with closing(
                sqlite3.connect(rollback_state / "memory" / "ops.sqlite")
            ) as connection:
                value = connection.execute(
                    "SELECT value FROM rollback_sentinel"
                ).fetchone()[0]
            self.assertEqual("pre-upgrade-v3", value)


if __name__ == "__main__":
    unittest.main()
