#!/usr/bin/env python3
"""Reject low-noise commit identity privacy regressions.

The diagnostic output intentionally contains only commit IDs, roles, and bounded
categories.  It never prints a name or email address.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


GITHUB_PRIVACY_DOMAINS = {
    "github.com",
    "noreply.github.com",
    "users.noreply.github.com",
}
ZERO_SHA = "0" * 40


@dataclass(frozen=True)
class IdentityIssue:
    commit: str
    role: str
    category: str

    def diagnostic(self) -> str:
        return f"commit {self.commit} {self.role}: {self.category}"


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )


def _commit_exists(repo: Path, revision: str) -> bool:
    if not revision or revision == ZERO_SHA:
        return False
    return (
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{revision}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def commits_in_range(repo: Path, base: str, head: str) -> tuple[str, ...]:
    if not _commit_exists(repo, head):
        raise ValueError("head is not an available commit")
    revision = f"{base}..{head}" if _commit_exists(repo, base) else head
    return tuple(
        commit
        for commit in _git(repo, "rev-list", "--reverse", revision).splitlines()
        if commit
    )


def _split_email(email: str) -> tuple[str, str]:
    if "@" not in email:
        return email, ""
    return tuple(email.rsplit("@", 1))  # type: ignore[return-value]


def _is_local_machine_email(email: str) -> bool:
    _local, domain = _split_email(email.casefold())
    return domain in {"local", "localhost"} or domain.endswith(".local")


def _is_github_privacy_email(email: str) -> bool:
    _local, domain = _split_email(email.casefold())
    return domain in GITHUB_PRIVACY_DOMAINS


def audit_range(
    repo: Path,
    *,
    base: str,
    head: str,
    protected_names: frozenset[str] = frozenset(),
) -> tuple[IdentityIssue, ...]:
    folded_names = {name.casefold() for name in protected_names}
    issues: list[IdentityIssue] = []
    for commit in commits_in_range(repo, base, head):
        fields = _git(
            repo,
            "show",
            "-s",
            "--format=%an%x00%ae%x00%cn%x00%ce",
            commit,
        ).rstrip("\n").split("\x00")
        if len(fields) != 4:
            issues.append(IdentityIssue(commit, "metadata", "malformed-identity"))
            continue
        for role, name, email in (
            ("author", fields[0], fields[1]),
            ("committer", fields[2], fields[3]),
        ):
            if _is_local_machine_email(email):
                issues.append(IdentityIssue(commit, role, "local-machine-email"))
            elif name.casefold() in folded_names and not _is_github_privacy_email(email):
                issues.append(
                    IdentityIssue(commit, role, "protected-name-with-non-noreply-email")
                )
    return tuple(issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--protected-name", action="append", default=[])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    try:
        issues = audit_range(
            args.repo,
            base=args.base,
            head=args.head,
            protected_names=frozenset(args.protected_name),
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        print(f"commit identity audit could not run: {type(exc).__name__}", file=sys.stderr)
        return 2
    if issues:
        print("commit identity privacy audit failed", file=sys.stderr)
        for issue in issues:
            print(issue.diagnostic(), file=sys.stderr)
        return 1
    print("commit identity privacy audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
