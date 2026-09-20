#!/usr/bin/env python3
"""Validate Claude structured output and render a deterministic PR review."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


SEVERITIES = {"P0", "P1"}
FORBIDDEN_TEXT = (
    "ai-coauthor:",
    "pr-watcher-head-sha:",
    "pr-review-model:",
    "结论：",
    "review_validation_feedback",
    "chain-of-thought",
    "system prompt",
    "output format",
)


class ReviewValidationError(ValueError):
    pass


def _require_single_line(value: object, label: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ReviewValidationError(f"{label} must be a string")
    text = value.strip()
    if not text or len(text) > max_length or "\n" in text or "\r" in text:
        raise ReviewValidationError(
            f"{label} must be a non-empty single line of at most {max_length} characters"
        )
    return text


def _contains_forbidden_text(value: str) -> bool:
    lowered = value.lower()
    return any(token.lower() in lowered for token in FORBIDDEN_TEXT)


def load_changed_files(path: Path) -> set[str]:
    files = {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    if not files:
        raise ReviewValidationError("changed file list is empty")
    return files


def load_findings(path: Path, changed_files: set[str]) -> list[dict[str, object]]:
    try:
        wrapper = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewValidationError("Claude output is not valid JSON") from exc

    if not isinstance(wrapper, dict) or wrapper.get("is_error") is True:
        raise ReviewValidationError("Claude result wrapper reports an error")
    structured = wrapper.get("structured_output")
    if not isinstance(structured, dict):
        raise ReviewValidationError("Claude result has no structured_output object")
    findings = structured.get("findings")
    if not isinstance(findings, list) or len(findings) > 20:
        raise ReviewValidationError("findings must be an array with at most 20 entries")

    validated: list[dict[str, object]] = []
    seen: set[tuple[str, int, str]] = set()
    for index, finding in enumerate(findings):
        label = f"findings[{index}]"
        if not isinstance(finding, dict):
            raise ReviewValidationError(f"{label} must be an object")
        if set(finding) != {"severity", "title", "file", "line", "body"}:
            raise ReviewValidationError(f"{label} has unexpected or missing fields")

        severity = finding.get("severity")
        if severity not in SEVERITIES:
            raise ReviewValidationError(f"{label}.severity must be P0 or P1")
        title = _require_single_line(finding.get("title"), f"{label}.title", 140)
        file_name = _require_single_line(finding.get("file"), f"{label}.file", 500)
        body = _require_single_line(finding.get("body"), f"{label}.body", 1000)
        line = finding.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ReviewValidationError(f"{label}.line must be a positive integer")
        if file_name not in changed_files:
            raise ReviewValidationError(f"{label}.file is not changed by this PR: {file_name}")
        if _contains_forbidden_text(title) or _contains_forbidden_text(body):
            raise ReviewValidationError(f"{label} contains caller-owned or process text")
        if re.search(r"(^|\s)P[23](\s|$)", title, flags=re.IGNORECASE):
            raise ReviewValidationError(f"{label}.title contains a forbidden severity")

        dedupe_key = (file_name, line, title)
        if dedupe_key in seen:
            raise ReviewValidationError(f"{label} duplicates an earlier finding")
        seen.add(dedupe_key)
        validated.append(
            {
                "severity": severity,
                "title": title,
                "file": file_name,
                "line": line,
                "body": body,
            }
        )

    return validated


def render(findings: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for finding in findings:
        parts.append(
            f"### {finding['severity']} — {finding['title']}\n\n"
            f"`{finding['file']}:{finding['line']}`\n\n"
            f"{finding['body']}"
        )
    if not parts:
        parts.append("未发现 P0/P1 级别问题。")
    conclusion = "结论：❌ 暂不可合并" if any(f["severity"] == "P0" for f in findings) else "结论：✅ 可以合并"
    return "\n\n".join(parts) + f"\n\n{conclusion}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--changed-files", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        changed_files = load_changed_files(args.changed_files)
        findings = load_findings(args.input, changed_files)
        args.output.write_text(render(findings), encoding="utf-8")
    except (OSError, ReviewValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"REVIEW_FINDING_COUNT={len(findings)}")
    print("REVIEW_RENDER=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
