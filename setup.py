from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py


ROOT = Path(__file__).resolve().parent
SKILL_FILES = (
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


class build_py(_build_py):
    def run(self):
        super().run()
        package_root = Path(self.build_lib) / "a2a_superhub"
        skill_source = ROOT / "skills" / "operate-a2a-superhub"
        skill_target = package_root / "_skill" / "operate-a2a-superhub"
        for relative in SKILL_FILES:
            target = skill_target / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(skill_source / relative, target)
        compatibility = __import__("json").loads(
            (skill_source / "references" / "compatibility.json").read_text(encoding="utf-8")
        )
        contracts_target = package_root / "_contracts"
        for relative in compatibility["contractFiles"]:
            target = contracts_target / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)


setup(cmdclass={"build_py": build_py})
