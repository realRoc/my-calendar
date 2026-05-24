"""Parse + validate a mycalfix://fix?... URL for launch_fix.sh.

Used by both the in-repo `scripts/launch_fix.sh` and the bundled copy at
`~/Applications/MyCalFix.app/Contents/Resources/launch_fix.sh`.

CLI contract:
  python3 parse_fix_url.py 'mycalfix://fix?...'

Stdout is shell-safe `key=shlex_quoted_value` lines (or `URL_ERROR=...` on
failure). Exit code is always 0; the caller checks for URL_ERROR.

Security: every field is untrusted. All four prompt-bound fields (`pr`,
`branch`, `comment`, `repo`) are validated:

  - `pr` / `comment` must be anchored GitHub URLs (host == github.com,
    pull-only, no trailing junk) pointing at the same repo + PR number.
  - `branch` passes a conservative GitHub branch-name whitelist (alnum +
    `. _ / -`, no leading `- / .`, no `..`, no `//`, no `@{`, ≤200 chars).
  - Every field is rejected if it contains ASCII control characters
    (`%0A`, `%09`, etc.) — those decode through `parse_qs` and would
    otherwise smuggle prompt-injection newlines into the `claude` prompt.
"""

from __future__ import annotations

import re
import shlex
import sys
import urllib.parse


# Anchored + GitHub-only + pull-only. Trailing junk (incl. %0A-decoded
# newlines) cannot pass `^...$`.
COMMENT_RE = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:#issuecomment-\d+)?$"
)
PR_RE = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)$")
BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]+")


def _has_control_chars(s: str) -> bool:
    return any(ord(c) < 0x20 for c in s)


def _is_valid_branch(name: str) -> bool:
    """Conservative GitHub-shaped branch-name whitelist.

    Stricter than `git check-ref-format --branch` in some places (e.g. we
    refuse leading `.`, since GitHub rejects it too) and looser in others.
    The aim is "anything GitHub UI lets you push" — not full git ref
    grammar — because the launcher only checks out GitHub branches.
    """
    if not name or len(name) > 200:
        return False
    if _has_control_chars(name):
        return False
    if not BRANCH_RE.fullmatch(name):
        return False
    if name.startswith(("-", "/", ".")) or name.endswith("/"):
        return False
    if ".." in name or "//" in name or "@{" in name:
        return False
    return True


def parse_and_validate(url: str) -> dict[str, str]:
    """Return {field: value, ...}. URL_ERROR key signals validation failure
    (the dict will only contain that one key in that case)."""
    p = urllib.parse.urlparse(url)
    if (p.scheme or "").lower() != "mycalfix":
        return {"URL_ERROR": f"URL scheme 不是 mycalfix: {p.scheme!r}"}
    if (p.netloc or "").lower() != "fix":
        return {"URL_ERROR": f"URL action 不是 fix: {p.netloc!r}"}

    q = urllib.parse.parse_qs(p.query, keep_blank_values=False)

    def first(k: str) -> str:
        v = q.get(k, [""])
        return v[0] if v else ""

    repo = first("repo")
    pr = first("pr")
    comment = first("comment")
    branch = first("branch")

    if repo and _has_control_chars(repo):
        return {"URL_ERROR": "repo 含控制字符，拒绝执行"}

    if pr:
        if _has_control_chars(pr):
            return {"URL_ERROR": "pr 含控制字符，拒绝执行"}
        pr_m = PR_RE.match(pr)
        if not pr_m or pr_m.group(1) != repo:
            return {"URL_ERROR": f"pr 不是合法 GitHub PR URL 或与 repo 不一致: pr={pr!r} repo={repo!r}"}
        pr_number = pr_m.group(2)
    else:
        pr_number = None

    if branch and not _is_valid_branch(branch):
        return {"URL_ERROR": f"branch 名非法或含控制字符: {branch!r}"}

    if comment:
        if _has_control_chars(comment):
            return {"URL_ERROR": "comment 含控制字符（换行/制表符等），拒绝执行"}
        c_m = COMMENT_RE.match(comment)
        if not c_m:
            return {"URL_ERROR": f"comment 不是合法的 GitHub PR comment URL: {comment!r}"}
        if c_m.group(1) != repo:
            return {"URL_ERROR": f"comment repo ({c_m.group(1)!r}) 与 repo ({repo!r}) 不一致"}
        if pr_number is not None and c_m.group(2) != pr_number:
            return {"URL_ERROR": f"comment PR #{c_m.group(2)} 与 pr #{pr_number} 不一致"}

    origin_cwd = first("origin_cwd")
    if origin_cwd and _has_control_chars(origin_cwd):
        return {"URL_ERROR": "origin_cwd 含控制字符（换行/制表符等），拒绝执行"}

    return {
        "repo": repo,
        "branch": branch,
        "comment": comment,
        "pr": pr,
        "origin_cwd": origin_cwd,
    }


def emit(result: dict[str, str]) -> None:
    for k, v in result.items():
        sys.stdout.write(f"{k}={shlex.quote(v)}\n")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        emit({"URL_ERROR": "parse_fix_url.py: missing URL argument"})
        return 0
    emit(parse_and_validate(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
