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

    def test_installed_pre_push_can_skip_only_review_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)

            install = subprocess.run(
                ["bash", str(ROOT / "scripts" / "install_git_hook.sh"), "--force"],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)

            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            hook = home / ".config" / "my-calendar" / "git-hooks" / "pre-push"
            env["MY_CALENDAR_PR_SKIP_PRE_PUSH_REVIEW"] = "1"
            payload = (
                "refs/heads/feature "
                "1111111111111111111111111111111111111111 "
                "refs/heads/feature "
                "0000000000000000000000000000000000000000\n"
            )
            result = subprocess.run(
                ["bash", str(hook), "origin", "git@github.com:realRoc/my-calendar.git"],
                cwd=str(repo),
                env=env,
                input=payload,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            log = home / ".config" / "my-calendar" / "git-hooks" / "logs" / "trigger.log"
            self.assertIn("skip review trigger", log.read_text(encoding="utf-8"))


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


class LightPrHelperTests(unittest.TestCase):
    def test_default_flow_claims_current_session_review_and_skips_pre_push_review(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = tmp / "repo"
            repo.mkdir()
            fake = tmp / "fake-bin"
            fake.mkdir()
            push_env = tmp / "push.env"
            hook_calls = tmp / "hook.calls"
            claim_calls = tmp / "claim.calls"
            my_calendar = tmp / "my-calendar"
            (my_calendar / "scripts").mkdir(parents=True)

            _write_exe(
                fake / "git",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    case "$1" in
                      rev-parse)
                        if [[ "$2" == "--show-toplevel" ]]; then
                          echo "$FAKE_REPO_ROOT"
                          exit 0
                        fi
                        if [[ "$2" == "--verify" ]]; then
                          exit 1
                        fi
                        ;;
                      branch)
                        echo feature/current-session
                        exit 0
                        ;;
                      remote)
                        echo git@github.com:realRoc/my-calendar.git
                        exit 0
                        ;;
                      status)
                        exit 0
                        ;;
                      log)
                        if [[ "$*" == *"--pretty=%s"* ]]; then
                          echo "fix(pr): current session review"
                        else
                          echo "- fix(pr): current session review"
                        fi
                        exit 0
                        ;;
                      diff)
                        echo " scripts/example.py | 1 +"
                        exit 0
                        ;;
                      show)
                        echo " scripts/example.py | 1 +"
                        exit 0
                        ;;
                      fetch)
                        exit 0
                        ;;
                      push)
                        printf '%s\\n' "${MY_CALENDAR_PR_SKIP_PRE_PUSH_REVIEW:-}" > "$PUSH_ENV_OUT"
                        exit 0
                        ;;
                    esac
                    echo "unexpected git args: $*" >&2
                    exit 99
                """),
            )
            _write_exe(
                fake / "gh",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    if [[ "$1" == "repo" && "$2" == "view" ]]; then
                      echo '{"nameWithOwner":"realRoc/my-calendar","defaultBranchRef":{"name":"main"}}'
                      exit 0
                    fi
                    if [[ "$1" == "pr" && "$2" == "list" ]]; then
                      echo '[]'
                      exit 0
                    fi
                    if [[ "$1" == "pr" && "$2" == "create" ]]; then
                      echo 'https://github.com/realRoc/my-calendar/pull/42'
                      exit 0
                    fi
                    echo "unexpected gh args: $*" >&2
                    exit 98
                """),
            )
            _write_exe(
                fake / "jq",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    expr=""
                    for arg in "$@"; do
                      expr="$arg"
                    done
                    case "$expr" in
                      .nameWithOwner) echo realRoc/my-calendar ;;
                      .defaultBranchRef.name) echo main ;;
                      *'select(.state == "OPEN")'*) ;;
                      *'select(.state != "OPEN")'*) ;;
                      *) echo "unexpected jq expr: $expr" >&2; exit 97 ;;
                    esac
                """),
            )
            _write_exe(
                tmp / "pr-created",
                f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {hook_calls}\n",
            )
            (my_calendar / "scripts" / "pr_session_review.py").write_text(
                textwrap.dedent(f"""\
                    import pathlib
                    import sys

                    pathlib.Path({str(claim_calls)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
                    print("PR_SESSION_CLAIM=ok")
                """),
                encoding="utf-8",
            )

            body = tmp / "body.md"
            body.write_text("## 解决什么问题\n\n- test\n", encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{fake}:/usr/bin:/bin"
            env["FAKE_REPO_ROOT"] = str(repo)
            env["PUSH_ENV_OUT"] = str(push_env)
            env["MY_CALENDAR_HOME"] = str(my_calendar)
            env["MY_CALENDAR_PR_CREATED_HOOK"] = str(tmp / "pr-created")

            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / ".agents" / "skills" / "pr" / "scripts" / "light_pr.sh"),
                    "--title",
                    "fix(pr): current session review",
                    "--body-file",
                    str(body),
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("PR_URL=https://github.com/realRoc/my-calendar/pull/42", result.stdout)
            self.assertIn("MY_CALENDAR_SESSION_CLAIM=checkout:", result.stdout)
            self.assertNotIn("MY_CALENDAR_TRIGGER=", result.stdout)
            self.assertEqual(push_env.read_text(encoding="utf-8").strip(), "1")
            self.assertIn("--claim", claim_calls.read_text(encoding="utf-8"))
            self.assertFalse(hook_calls.exists())

    def test_default_flow_aborts_when_current_session_claim_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = tmp / "repo"
            repo.mkdir()
            fake = tmp / "fake-bin"
            fake.mkdir()
            push_env = tmp / "push.env"
            my_calendar = tmp / "my-calendar"
            (my_calendar / "scripts").mkdir(parents=True)

            _write_exe(
                fake / "git",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    case "$1" in
                      rev-parse)
                        if [[ "$2" == "--show-toplevel" ]]; then
                          echo "$FAKE_REPO_ROOT"
                          exit 0
                        fi
                        if [[ "$2" == "--verify" ]]; then
                          exit 1
                        fi
                        ;;
                      branch)
                        echo feature/current-session
                        exit 0
                        ;;
                      remote)
                        echo git@github.com:realRoc/my-calendar.git
                        exit 0
                        ;;
                      status)
                        exit 0
                        ;;
                      log)
                        if [[ "$*" == *"--pretty=%s"* ]]; then
                          echo "fix(pr): current session review"
                        else
                          echo "- fix(pr): current session review"
                        fi
                        exit 0
                        ;;
                      diff|show)
                        echo " scripts/example.py | 1 +"
                        exit 0
                        ;;
                      fetch)
                        exit 0
                        ;;
                      push)
                        printf '%s\\n' "${MY_CALENDAR_PR_SKIP_PRE_PUSH_REVIEW:-}" > "$PUSH_ENV_OUT"
                        exit 0
                        ;;
                    esac
                    echo "unexpected git args: $*" >&2
                    exit 99
                """),
            )
            _write_exe(
                fake / "gh",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    if [[ "$1" == "repo" && "$2" == "view" ]]; then
                      echo '{"nameWithOwner":"realRoc/my-calendar","defaultBranchRef":{"name":"main"}}'
                      exit 0
                    fi
                    if [[ "$1" == "pr" && "$2" == "list" ]]; then
                      echo '[]'
                      exit 0
                    fi
                    if [[ "$1" == "pr" && "$2" == "create" ]]; then
                      echo 'https://github.com/realRoc/my-calendar/pull/42'
                      exit 0
                    fi
                    echo "unexpected gh args: $*" >&2
                    exit 98
                """),
            )
            _write_exe(
                fake / "jq",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    expr=""
                    for arg in "$@"; do
                      expr="$arg"
                    done
                    case "$expr" in
                      .nameWithOwner) echo realRoc/my-calendar ;;
                      .defaultBranchRef.name) echo main ;;
                      *'select(.state == "OPEN")'*) ;;
                      *'select(.state != "OPEN")'*) ;;
                      *) echo "unexpected jq expr: $expr" >&2; exit 97 ;;
                    esac
                """),
            )
            (my_calendar / "scripts" / "pr_session_review.py").write_text(
                textwrap.dedent("""\
                    import sys
                    print("ERROR: review already pending", file=sys.stderr)
                    raise SystemExit(13)
                """),
                encoding="utf-8",
            )

            body = tmp / "body.md"
            body.write_text("## 解决什么问题\n\n- test\n", encoding="utf-8")

            env = os.environ.copy()
            env["PATH"] = f"{fake}:/usr/bin:/bin"
            env["FAKE_REPO_ROOT"] = str(repo)
            env["PUSH_ENV_OUT"] = str(push_env)
            env["MY_CALENDAR_HOME"] = str(my_calendar)

            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / ".agents" / "skills" / "pr" / "scripts" / "light_pr.sh"),
                    "--title",
                    "fix(pr): current session review",
                    "--body-file",
                    str(body),
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MY_CALENDAR_SESSION_CLAIM=failed", result.stdout)
            self.assertIn("aborting before posting a duplicate review", result.stderr)
            self.assertEqual(push_env.read_text(encoding="utf-8").strip(), "1")

    def test_trigger_only_does_not_require_gh_or_jq(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            fake = tmp / "fake-bin"
            fake.mkdir()
            _write_exe(fake / "gh", "#!/usr/bin/env bash\necho 'gh should not be called' >&2\nexit 99\n")
            _write_exe(fake / "jq", "#!/usr/bin/env bash\necho 'jq should not be called' >&2\nexit 98\n")

            calls = tmp / "hook.calls"
            hook = tmp / "pr-created"
            _write_exe(
                hook,
                f"""#!/usr/bin/env bash
printf '%s\\n' "$1" "$2" > {calls}
""",
            )

            env = os.environ.copy()
            env["PATH"] = f"{fake}:/usr/bin:/bin"
            env["MY_CALENDAR_PR_CREATED_HOOK"] = str(hook)

            pr_url = "https://github.com/realRoc/my-calendar/pull/42"
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / ".agents" / "skills" / "pr" / "scripts" / "light_pr.sh"),
                    "--trigger-only",
                    pr_url,
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"PR_URL={pr_url}", result.stdout)
            self.assertIn("MY_CALENDAR_TRIGGER=hook:", result.stdout)
            got_url, got_root = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(got_url, pr_url)
            self.assertEqual(Path(got_root).resolve(), repo.resolve())

    def test_trigger_only_rejects_non_canonical_pr_url(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=str(repo), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

            hook_calls = tmp / "hook.calls"
            hook = tmp / "pr-created"
            _write_exe(hook, f"#!/usr/bin/env bash\necho called > {hook_calls}\n")

            env = os.environ.copy()
            env["MY_CALENDAR_PR_CREATED_HOOK"] = str(hook)

            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / ".agents" / "skills" / "pr" / "scripts" / "light_pr.sh"),
                    "--trigger-only",
                    "https://github.com/realRoc/my-calendar/pull/42/extra",
                ],
                cwd=str(repo),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("--trigger-only requires a GitHub PR URL", result.stderr)
            self.assertFalse(hook_calls.exists())


class PrReviewTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_terminal_bridge = os.environ.get("MY_CALENDAR_PR_TERMINAL_BRIDGE")
        os.environ["MY_CALENDAR_PR_TERMINAL_BRIDGE"] = "0"

    def tearDown(self) -> None:
        if self._old_terminal_bridge is None:
            os.environ.pop("MY_CALENDAR_PR_TERMINAL_BRIDGE", None)
        else:
            os.environ["MY_CALENDAR_PR_TERMINAL_BRIDGE"] = self._old_terminal_bridge

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

    def test_review_trigger_bridges_codex_desktop_to_terminal_before_watcher(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            origin = tmp / "checkout"
            origin.mkdir()
            fake = tmp / "fake-bin"
            fake.mkdir()
            open_args = tmp / "open.args"
            command_copy = tmp / "bridge.command"
            _write_exe(
                fake / "open",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    last=""
                    for arg in "$@"; do
                      last="$arg"
                    done
                    printf '%s\\n' "$@" > "$OPEN_ARGS_OUT"
                    cp "$last" "$OPEN_CAPTURE_OUT"
                """),
            )

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"
            env["MY_CALENDAR_PR_TERMINAL_BRIDGE"] = "1"
            env["OPEN_ARGS_OUT"] = str(open_args)
            env["OPEN_CAPTURE_OUT"] = str(command_copy)

            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "pr_review_trigger.sh"),
                    "--source",
                    "pr-created",
                    "https://github.com/realRoc/my-calendar/pull/42",
                    str(origin),
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("terminal bridge launched", result.stdout)
            self.assertEqual(
                open_args.read_text(encoding="utf-8").splitlines()[:3],
                ["-g", "-a", "Terminal"],
            )
            command = command_copy.read_text(encoding="utf-8")
            self.assertIn("export MY_CALENDAR_PR_TERMINAL_BRIDGE=0", command)
            self.assertIn("--source pr-created:terminal-bridge", command)
            self.assertIn("https://github.com/realRoc/my-calendar/pull/42", command)
            self.assertIn(str(origin), command)
            debounce_dir = home / ".config" / "my-calendar" / "git-hooks" / "review-triggers"
            self.assertFalse(list(debounce_dir.glob("*.stamp")))

    def test_review_trigger_auto_bridges_codex_desktop_bundle_identifier(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            home = tmp / "home"
            home.mkdir()
            fake = tmp / "fake-bin"
            fake.mkdir()
            open_args = tmp / "open.args"
            command_copy = tmp / "bridge.command"
            _write_exe(
                fake / "open",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    last=""
                    for arg in "$@"; do
                      last="$arg"
                    done
                    printf '%s\\n' "$@" > "$OPEN_ARGS_OUT"
                    cp "$last" "$OPEN_CAPTURE_OUT"
                """),
            )

            env = os.environ.copy()
            env.pop("MY_CALENDAR_PR_TERMINAL_BRIDGE", None)
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"
            env["__CFBundleIdentifier"] = "com.openai.codex"
            env["OPEN_ARGS_OUT"] = str(open_args)
            env["OPEN_CAPTURE_OUT"] = str(command_copy)

            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "pr_review_trigger.sh"),
                    "https://github.com/realRoc/my-calendar/pull/42",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("terminal bridge launched", result.stdout)
            self.assertEqual(
                open_args.read_text(encoding="utf-8").splitlines()[:3],
                ["-g", "-a", "Terminal"],
            )
            command = command_copy.read_text(encoding="utf-8")
            self.assertIn("--source manual:terminal-bridge", command)

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


class PrRecordReviewTriggerTests(unittest.TestCase):
    def test_terminal_record_bridge_auto_closes_successful_window(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = tmp / "repo"
            scripts = root / "scripts"
            venv_bin = root / ".venv" / "bin"
            scripts.mkdir(parents=True)
            venv_bin.mkdir(parents=True)
            shutil.copy2(ROOT / "scripts" / "pr_record_review_trigger.sh", scripts / "pr_record_review_trigger.sh")
            (scripts / "pr_record_review_trigger.sh").chmod(0o755)
            (scripts / "pr_session_review.py").write_text("# fake recorder target\n", encoding="utf-8")
            _write_exe(venv_bin / "python", "#!/usr/bin/env bash\nexit 0\n")

            home = tmp / "home"
            home.mkdir()
            fake = tmp / "fake-bin"
            fake.mkdir()
            open_args = tmp / "open.args"
            command_copy = tmp / "bridge.command"
            _write_exe(
                fake / "open",
                textwrap.dedent("""\
                    #!/usr/bin/env bash
                    last=""
                    for arg in "$@"; do
                      last="$arg"
                    done
                    printf '%s\\n' "$@" > "$OPEN_ARGS_OUT"
                    cp "$last" "$OPEN_CAPTURE_OUT"
                    status="$(awk '/^printf .* > .*\\.status$/ {print $NF; exit}' "$last")"
                    if [[ -z "$status" ]]; then
                      echo "status path not found in generated command" >&2
                      exit 93
                    fi
                    mkdir -p "$(dirname "$status")"
                    printf 'rc=0\\n' > "$status"
                """),
            )

            env = os.environ.copy()
            env["HOME"] = str(home)
            env["PATH"] = f"{fake}:{env['PATH']}"
            env["MY_CALENDAR_PR_RECORD_TERMINAL_BRIDGE"] = "1"
            env["OPEN_ARGS_OUT"] = str(open_args)
            env["OPEN_CAPTURE_OUT"] = str(command_copy)

            result = subprocess.run(
                [
                    "bash",
                    str(scripts / "pr_record_review_trigger.sh"),
                    "--timeout",
                    "2",
                    "https://github.com/realRoc/my-calendar/pull/42",
                    "https://github.com/realRoc/my-calendar/pull/42#issuecomment-123",
                    str(root),
                ],
                cwd=str(root),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("MY_CALENDAR_RECORD=terminal-bridge:success", result.stdout)
            self.assertEqual(
                open_args.read_text(encoding="utf-8").splitlines()[:3],
                ["-g", "-a", "Terminal"],
            )
            command = command_copy.read_text(encoding="utf-8")
            self.assertIn('MY_CALENDAR_PR_RECORD_TERMINAL_AUTO_CLOSE:-1', command)
            self.assertIn('auto_close_terminal_bridge_if_success "$rc"', command)
            self.assertIn('[[ "$rc" -eq 0 ]] || return 0', command)
            self.assertIn("if (count of tabs of w) is 1 then", command)
            self.assertIn("close w saving no", command)
            self.assertNotIn("close t saving no", command)
            syntax = subprocess.run(
                ["bash", "-n", str(command_copy)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(syntax.returncode, 0, syntax.stderr)


if __name__ == "__main__":
    unittest.main()
