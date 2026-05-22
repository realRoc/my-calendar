"""Parse + validate a mycalfix://fix?... URL for launch_fix.sh.

Used by both the in-repo `scripts/launch_fix.sh` and the bundled copy at
`~/Applications/MyCalFix.app/Contents/Resources/launch_fix.sh`.

CLI contract:
  python3 parse_fix_url.py 'mycalfix://fix?...'

Stdout is shell-safe `key=shlex_quoted_value` lines (or `URL_ERROR=...` on
failure). Exit code is always 0; the caller checks for URL_ERROR.

Security: every field is untrusted. `comment` is constrained to a GitHub
`https://github.com/<owner>/<repo>/pull/<n>(#issuecomment-<id>)?` URL that
points at the same repo + PR as the `repo` and `pr` fields, with no control
characters — because the launcher feeds it into a `claude` prompt and we
don't want an attacker-controlled URL to redirect Claude at a malicious page
or smuggle prompt-injection newlines.
"""

from __future__ import annotations

import re
import shlex
import sys
import urllib.parse


COMMENT_RE = re.compile(
    r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)(?:#issuecomment-\d+)?$"
)
PR_RE = re.compile(r"^https?://[^/]+/([^/]+/[^/]+)/(?:pull|issues)/(\d+)")


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

    pr_m = PR_RE.match(pr) if pr else None
    if pr and (not pr_m or pr_m.group(1) != repo):
        return {"URL_ERROR": f"pr URL 与 repo 不一致: pr={pr!r} repo={repo!r}"}
    pr_number = pr_m.group(2) if pr_m else None

    if comment:
        if any(ord(ch) < 0x20 for ch in comment):
            return {"URL_ERROR": "comment 含控制字符（换行/制表符等），拒绝执行"}
        c_m = COMMENT_RE.match(comment)
        if not c_m:
            return {"URL_ERROR": f"comment 不是合法的 GitHub PR comment URL: {comment!r}"}
        if c_m.group(1) != repo:
            return {"URL_ERROR": f"comment repo ({c_m.group(1)!r}) 与 repo ({repo!r}) 不一致"}
        if pr_number is not None and c_m.group(2) != pr_number:
            return {"URL_ERROR": f"comment PR #{c_m.group(2)} 与 pr #{pr_number} 不一致"}

    return {k: first(k) for k in ("repo", "branch", "comment", "pr", "origin_cwd")}


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
