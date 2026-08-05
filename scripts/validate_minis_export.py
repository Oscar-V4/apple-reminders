#!/usr/bin/env python3
"""Validate the allowlisted, portable MinisSkills export."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPORT = ROOT / "minis" / "apple-reminders"
ALLOWED_FILES = {Path("SKILL.md"), Path("evals/evals.json")}
FORBIDDEN_TEXT = {
    "../../scripts",
    ".codex-plugin",
    "/Users/",
    "group.com.apple.reminders",
    "reminders_adapter.py",
    "remkit_attach_image",
    "ReminderKit",
    "SQLite",
}


def validation_error(message: str) -> int:
    print(f"Minis export validation failed: {message}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    export = Path(args[0]).expanduser().resolve() if args else DEFAULT_EXPORT
    if not export.is_dir():
        return validation_error(f"directory not found: {export}")

    files = {
        path.relative_to(export)
        for path in export.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if files != ALLOWED_FILES:
        unexpected = sorted(str(path) for path in files - ALLOWED_FILES)
        missing = sorted(str(path) for path in ALLOWED_FILES - files)
        return validation_error(f"unexpected={unexpected}, missing={missing}")
    if any((export / path).is_symlink() for path in ALLOWED_FILES):
        return validation_error("symlinks are not allowed")

    skill_path = export / "SKILL.md"
    skill = skill_path.read_text(encoding="utf-8")
    if len(skill.splitlines()) > 500:
        return validation_error("SKILL.md exceeds 500 lines")
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", skill, flags=re.DOTALL)
    if not frontmatter:
        return validation_error("SKILL.md frontmatter is missing or malformed")
    metadata = frontmatter.group(1)
    if not re.search(r"(?m)^name:\s*apple-reminders\s*$", metadata):
        return validation_error("frontmatter name must be apple-reminders")
    if not re.search(r"(?m)^description:\s*(?:>|.+)$", metadata):
        return validation_error("frontmatter description is missing")

    combined_text = skill
    eval_path = export / "evals" / "evals.json"
    try:
        eval_payload = json.loads(eval_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return validation_error(f"evals/evals.json is invalid JSON: {exc}")
    combined_text += "\n" + json.dumps(eval_payload, ensure_ascii=False)
    folded_text = combined_text.casefold()
    found_forbidden = sorted(
        token for token in FORBIDDEN_TEXT if token.casefold() in folded_text
    )
    if found_forbidden:
        return validation_error(f"private/local tokens found: {found_forbidden}")

    if not isinstance(eval_payload, dict) or eval_payload.get("skill_name") != "apple-reminders":
        return validation_error("eval payload must name the apple-reminders skill")
    evals = eval_payload.get("evals")
    if not isinstance(evals, list) or not 2 <= len(evals) <= 5:
        return validation_error("evals must contain between 2 and 5 cases")
    ids: set[int] = set()
    for item in evals:
        if not isinstance(item, dict):
            return validation_error("every eval case must be an object")
        if not {"id", "prompt", "expected_output", "files", "assertions"} <= item.keys():
            return validation_error("an eval case is missing required fields")
        if not isinstance(item["id"], int) or item["id"] in ids:
            return validation_error("eval ids must be unique integers")
        ids.add(item["id"])
        if not isinstance(item["prompt"], str) or not item["prompt"].strip():
            return validation_error("every eval prompt must be non-empty")
        if not isinstance(item["expected_output"], str) or not item["expected_output"].strip():
            return validation_error("every expected_output must be non-empty")
        if not isinstance(item["files"], list) or not isinstance(item["assertions"], list):
            return validation_error("files and assertions must be lists")
        for assertion in item["assertions"]:
            if not isinstance(assertion, dict):
                return validation_error("every assertion must be an object")
            if assertion.get("type") not in {"contains", "not_contains"}:
                return validation_error("assertion type must be contains or not_contains")
            if not isinstance(assertion.get("text"), str) or not assertion["text"].strip():
                return validation_error("every assertion needs non-empty text")

    print(f"Minis export validation passed: {export}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
