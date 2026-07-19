import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from a2a_superhub.skill_package import (
    SkillInstallError,
    install_skill,
    resolve_codex_home,
    skill_source_path,
    uninstall_skill,
    validate_skill,
)


class SkillInstallTests(unittest.TestCase):
    def test_canonical_skill_validates(self):
        source = skill_source_path()
        result = validate_skill(source)
        self.assertTrue(result["valid"])
        self.assertEqual(result["skill"], "operate-a2a-superhub")

    def test_install_refuses_overwrite_and_uninstall_respects_ownership(self):
        with tempfile.TemporaryDirectory(prefix="a2a codex home ") as tmp:
            root = Path(tmp).resolve()
            protected = root / "state" / "token.json"
            protected.parent.mkdir(parents=True)
            protected.write_text("do-not-touch", encoding="utf-8")
            first = install_skill(target="codex", target_root=root)
            installed = Path(first["path"])
            self.assertTrue((installed / ".a2a-superhub-install.json").is_file())
            with self.assertRaises(SkillInstallError):
                install_skill(target="codex", target_root=root)
            user_file = installed / "user-note.txt"
            user_file.write_text("mine", encoding="utf-8")
            result = uninstall_skill(target="codex", target_root=root)
            self.assertEqual(result["removed"], True)
            self.assertTrue(user_file.is_file())
            self.assertEqual(protected.read_text(encoding="utf-8"), "do-not-touch")

    def test_force_makes_recoverable_backup(self):
        with tempfile.TemporaryDirectory(prefix="a2a-codex-") as tmp:
            root = Path(tmp).resolve()
            installed = root / "skills" / "operate-a2a-superhub"
            installed.mkdir(parents=True)
            (installed / "SKILL.md").write_text("user version", encoding="utf-8")
            result = install_skill(target="codex", target_root=root, force=True)
            backup = Path(result["backup"])
            self.assertTrue((backup / "SKILL.md").is_file())
            self.assertEqual((backup / "SKILL.md").read_text(encoding="utf-8"), "user version")

    def test_relative_or_unresolved_codex_home_is_rejected(self):
        with self.assertRaises(SkillInstallError):
            resolve_codex_home(Path("relative-target"))
        with patch.dict(os.environ, {"CODEX_HOME": "relative-env"}, clear=False):
            with self.assertRaises(SkillInstallError):
                resolve_codex_home(None)

    def test_malformed_ownership_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory(prefix="a2a-codex-") as tmp:
            root = Path(tmp).resolve()
            installed = Path(install_skill(target="codex", target_root=root)["path"])
            skill_before = (installed / "SKILL.md").read_bytes()
            manifest = installed / ".a2a-superhub-install.json"
            manifest.write_text('{"installer":"a2a-superhub","skill":"operate-a2a-superhub","files":[]}', encoding="utf-8")
            with self.assertRaises(SkillInstallError):
                uninstall_skill(target="codex", target_root=root)
            self.assertEqual(skill_before, (installed / "SKILL.md").read_bytes())

    def test_uninstall_preflights_late_escape_before_removing_earlier_files(self):
        with tempfile.TemporaryDirectory(prefix="a2a-codex-") as tmp, tempfile.TemporaryDirectory(prefix="a2a-outside-") as outside:
            root = Path(tmp).resolve()
            installed = Path(install_skill(target="codex", target_root=root)["path"])
            earlier = installed / "SKILL.md"
            late = installed / "scripts" / "smoke.py"
            outside_file = Path(outside) / "smoke.py"
            outside_file.write_text("outside", encoding="utf-8")
            late.unlink()
            try:
                late.symlink_to(outside_file)
            except OSError as exc:
                self.skipTest(f"file symlink unavailable: {exc}")
            with self.assertRaisesRegex(SkillInstallError, "escapes"):
                uninstall_skill(target="codex", target_root=root)
            self.assertTrue(earlier.is_file())
            self.assertEqual(outside_file.read_text(encoding="utf-8"), "outside")

    def test_runtime_cache_is_not_installed_payload(self):
        source = skill_source_path()
        cache = source / "scripts" / "__pycache__"
        cache.mkdir(exist_ok=True)
        debris = cache / "doctor.cpython-test.pyc"
        debris.write_bytes(b"not-bytecode")
        try:
            with tempfile.TemporaryDirectory(prefix="a2a-codex-") as tmp:
                installed = Path(install_skill(target="codex", target_root=Path(tmp).resolve())["path"])
                self.assertFalse((installed / "scripts" / "__pycache__").exists())
        finally:
            debris.unlink(missing_ok=True)
            try:
                cache.rmdir()
            except OSError:
                pass

    def test_force_install_rejects_existing_destination_link(self):
        with tempfile.TemporaryDirectory(prefix="a2a-codex-") as tmp:
            root = Path(tmp).resolve()
            skills = root / "skills"
            unrelated = skills / "unrelated-skill"
            unrelated.mkdir(parents=True)
            marker = unrelated / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            linked = skills / "operate-a2a-superhub"
            try:
                linked.symlink_to(unrelated, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            with self.assertRaisesRegex(SkillInstallError, "symlink or junction"):
                install_skill(target="codex", target_root=root, force=True)
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
