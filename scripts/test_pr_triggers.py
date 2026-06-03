"""Regression tests for local PR review trigger shell scripts.

Run with:
    .venv/bin/python -m unittest scripts.test_pr_triggers
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class InstallGitHookTests(unittest.TestCase):
    def test_installer_deploys_pr_created_hook_with_rendered_trigger_path(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td) / "home"
            home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_git_hook.sh"), "--force"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            hook = home / ".config" / "my-calendar" / "git-hooks" / "pr-created"
            self.assertTrue(hook.exists())
            self.assertTrue(os.access(hook, os.X_OK))
            body = hook.read_text(encoding="utf-8")
            self.assertNotIn("__PR_CREATED_TRIGGER_SCRIPT__", body)
            self.assertIn(str(ROOT / "scripts" / "pr_created_trigger.sh"), body)


class PrCreatedTriggerTests(unittest.TestCase):
    def test_extracts_pr_url_and_forwards_origin_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            scripts = tmp / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "pr_created_trigger.sh", scripts / "pr_created_trigger.sh")
            (scripts / "pr_created_trigger.sh").chmod(0o755)
            calls = tmp / "calls.log"
            _write_exe(
                scripts / "pr_review_trigger.sh",
                f"""#!/usr/bin/env bash
printf '%s\\n' "$@" >> {calls}
""",
            )
            home = tmp / "home"
            home.mkdir()
            origin = tmp / "checkout"
            origin.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = subprocess.run(
                [
                    "bash",
                    str(scripts / "pr_created_trigger.sh"),
                    "created: https://github.com/realRoc/my-calendar/pull/42",
                    str(origin),
                ],
                cwd=str(tmp),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    "--source",
                    "pr-created",
                    "https://github.com/realRoc/my-calendar/pull/42",
                    str(origin),
                ],
            )

    def test_ignores_text_without_github_pr_url(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            scripts = tmp / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts" / "pr_created_trigger.sh", scripts / "pr_created_trigger.sh")
            (scripts / "pr_created_trigger.sh").chmod(0o755)
            calls = tmp / "calls.log"
            _write_exe(scripts / "pr_review_trigger.sh", f"#!/usr/bin/env bash\necho called >> {calls}\n")
            home = tmp / "home"
            home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)

            result = subprocess.run(
                ["bash", str(scripts / "pr_created_trigger.sh"), "not a pr"],
                cwd=str(tmp),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(calls.exists())


class PrReviewTriggerTests(unittest.TestCase):
    def _fake_bin(self, tmp: Path, *, base: str = "main", default: str = "main", sha: str = "abc123") -> Path:
        fake = tmp / "fake-bin"
        fake.mkdir()
        _write_exe(fake / "gh", "#!/usr/bin/env bash\necho '{}'\n")
        _write_exe(
            fake / "jq",
            textwrap.dedent(f"""\
                #!/usr/bin/env bash
                expr="$2"
                case "$expr" in
                  *baseRefName*) echo {base!r} ;;
                  *defaultBranchRef*) echo {default!r} ;;
                  *headRefOid*) echo {sha!r} ;;
                  *url*) echo https://github.com/realRoc/my-calendar/pull/42 ;;
                  *) echo "" ;;
                esac
            """),
        )
        return fake

    def test_review_trigger_skips_non_default_base(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            fake = self._fake_bin(tmp, base="feature", default="main")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "pr_review_trigger.sh"), "https://github.com/realRoc/my-calendar/pull/42"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("base=feature", result.stdout)

    def test_review_trigger_debounces_same_pr_and_sha(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            fake = self._fake_bin(tmp, sha="abc123")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"
            debounce_dir = home / ".config" / "my-calendar" / "git-hooks" / "review-triggers"
            debounce_dir.mkdir(parents=True)
            key = subprocess.check_output(
                ["shasum", "-a", "256"],
                input=b"https://github.com/realRoc/my-calendar/pull/42@abc123",
            ).decode("utf-8").split()[0]
            (debounce_dir / f"{key}.stamp").write_text("existing\n", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "pr_review_trigger.sh"), "https://github.com/realRoc/my-calendar/pull/42"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skip duplicate review trigger", result.stdout)

    def test_review_trigger_skips_when_debounce_lock_is_busy(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            fake = self._fake_bin(tmp, sha="abc123")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"
            debounce_dir = home / ".config" / "my-calendar" / "git-hooks" / "review-triggers"
            debounce_dir.mkdir(parents=True)
            key = subprocess.check_output(
                ["shasum", "-a", "256"],
                input=b"https://github.com/realRoc/my-calendar/pull/42@abc123",
            ).decode("utf-8").split()[0]
            (debounce_dir / f"{key}.lock").mkdir()

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "pr_review_trigger.sh"), "https://github.com/realRoc/my-calendar/pull/42"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("debounce lock busy", result.stdout)

    def test_review_trigger_removes_stale_debounce_lock(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            fake = self._fake_bin(tmp, sha="abc123")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"
            env["MY_CALENDAR_PR_TRIGGER_LOCK_STALE_SECONDS"] = "0"
            debounce_dir = home / ".config" / "my-calendar" / "git-hooks" / "review-triggers"
            debounce_dir.mkdir(parents=True)
            key = subprocess.check_output(
                ["shasum", "-a", "256"],
                input=b"https://github.com/realRoc/my-calendar/pull/42@abc123",
            ).decode("utf-8").split()[0]
            (debounce_dir / f"{key}.lock").mkdir()
            (debounce_dir / f"{key}.stamp").write_text("existing\n", encoding="utf-8")

            result = subprocess.run(
                ["bash", str(ROOT / "scripts" / "pr_review_trigger.sh"), "https://github.com/realRoc/my-calendar/pull/42"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skip duplicate review trigger", result.stdout)
            self.assertFalse((debounce_dir / f"{key}.lock").exists())

    def test_review_trigger_removes_stamp_when_watcher_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = tmp / "repo"
            scripts = root / "scripts"
            venv_bin = root / ".venv" / "bin"
            scripts.mkdir(parents=True)
            venv_bin.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "pr_review_trigger.sh", scripts / "pr_review_trigger.sh")
            (scripts / "pr_review_trigger.sh").chmod(0o755)
            (scripts / "pr_watcher.py").write_text("# fake watcher target\n", encoding="utf-8")
            _write_exe(venv_bin / "python", "#!/usr/bin/env bash\nexit 7\n")

            home = tmp / "home"
            home.mkdir()
            fake = self._fake_bin(tmp, sha="abc123")
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"

            result = subprocess.run(
                ["bash", str(scripts / "pr_review_trigger.sh"), "https://github.com/realRoc/my-calendar/pull/42"],
                cwd=str(root),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("pr_watcher launched", result.stdout)

            log = home / ".config" / "my-calendar" / "git-hooks" / "logs" / "trigger.log"
            for _ in range(50):
                if log.exists() and "exited non-zero" in log.read_text(encoding="utf-8"):
                    break
                time.sleep(0.05)

            key = subprocess.check_output(
                ["shasum", "-a", "256"],
                input=b"https://github.com/realRoc/my-calendar/pull/42@abc123",
            ).decode("utf-8").split()[0]
            stamp = home / ".config" / "my-calendar" / "git-hooks" / "review-triggers" / f"{key}.stamp"
            self.assertFalse(stamp.exists())


if __name__ == "__main__":
    unittest.main()
