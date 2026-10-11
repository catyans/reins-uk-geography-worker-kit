import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install_skill.py"
spec = importlib.util.spec_from_file_location("install_skill", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SkillInstallTests(unittest.TestCase):
    def test_install_idempotence_force_and_uninstall(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "customer"
            target.mkdir()
            dest = target / ".agents/skills/reins-integrate"
            module.install(target, "codex", dry_run=True)
            self.assertFalse(dest.exists())
            module.install(target, "codex")
            self.assertTrue((dest / "SKILL.md").exists())
            module.install(target, "codex")
            (dest / "SKILL.md").write_text("customer edit")
            with self.assertRaises(ValueError):
                module.install(target, "codex")
            module.install(target, "codex", force=True)
            self.assertIn("hosted Reins", (dest / "SKILL.md").read_text())
            module.install(target, "codex", uninstall=True)
            self.assertFalse(dest.exists())

    def test_both_assistants_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "customer"
            outside = Path(folder) / "outside"
            target.mkdir()
            outside.mkdir()
            module.install(target, "both")
            self.assertTrue((target / ".agents/skills/reins-integrate/SKILL.md").exists())
            self.assertTrue((target / ".claude/skills/reins-integrate/SKILL.md").exists())
            module.install(target, "both", uninstall=True)
            # A symlink in the install path must not redirect writes outside the repo.
            (target / ".agents/skills").rename(target / "skills-old")
            (target / ".agents/skills").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlinked"):
                module.install(target, "codex")
            self.assertEqual(list(outside.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
