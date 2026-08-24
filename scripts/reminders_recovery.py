"""Scoped Apple Reminders recovery snapshots and bounded retention.

The interface is intentionally small: create a whole-container archive for
cross-store attachment repair, create one SQLite online backup for a
single-store mutation, or prune only strictly named plugin-owned backups.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sqlite3
import tarfile
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


BackupKind = Literal["container", "database"]

CONTAINER_PREFIX = "reminders-container-backup-"
DATABASE_PREFIX = "reminders-database-backup-"


@dataclass(frozen=True)
class RetentionPolicy:
    max_count: int
    max_bytes: int

    def __post_init__(self) -> None:
        if self.max_count < 0 or self.max_bytes < 0:
            raise ValueError("backup retention limits must be non-negative")


CONTAINER_POLICY = RetentionPolicy(max_count=2, max_bytes=300_000_000)
DATABASE_POLICY = RetentionPolicy(max_count=5, max_bytes=100_000_000)


_MANAGED_PATTERNS = {
    "container": re.compile(
        rf"^{re.escape(CONTAINER_PREFIX)}\d{{8}}-\d{{6}}(?:-\d+)?\.tgz$"
    ),
    "database": re.compile(
        rf"^{re.escape(DATABASE_PREFIX)}[a-z0-9-]+-\d{{8}}-\d{{6}}(?:-\d+)?\.sqlite$"
    ),
}


def _ensure_private_dir(path: Path, *, tighten_existing: bool = True) -> None:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not existed or tighten_existing:
        path.chmod(0o700)


def _safe_label(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:48] or "operation"


def _timestamp(now: dt.datetime | None) -> str:
    return (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def _available_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("unable to allocate a unique backup filename")


def _managed_backups(backup_dir: Path, kind: BackupKind) -> list[Path]:
    pattern = _MANAGED_PATTERNS[kind]
    if not backup_dir.is_dir():
        return []
    return [
        path
        for path in backup_dir.iterdir()
        if path.is_file() and not path.is_symlink() and pattern.fullmatch(path.name)
    ]


def prune_managed_backups(
    *,
    backup_dir: Path,
    kind: BackupKind,
    policy: RetentionPolicy,
    protected: set[Path] | None = None,
) -> dict[str, Any]:
    protected_paths = {path.resolve() for path in (protected or set())}
    candidates = sorted(
        _managed_backups(backup_dir, kind),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
        reverse=True,
    )
    retained: list[Path] = []
    removed: list[Path] = []
    retained_bytes = 0
    removed_bytes = 0

    for path in candidates:
        size = path.stat().st_size
        is_protected = path.resolve() in protected_paths
        within_count = len(retained) < policy.max_count
        within_bytes = retained_bytes + size <= policy.max_bytes
        if is_protected or (within_count and within_bytes):
            retained.append(path)
            retained_bytes += size
            continue
        path.unlink()
        removed.append(path)
        removed_bytes += size

    return {
        "kind": kind,
        "policy": {
            "max_count": policy.max_count,
            "max_bytes": policy.max_bytes,
        },
        "retained_count": len(retained),
        "retained_bytes": retained_bytes,
        "removed_count": len(removed),
        "removed_bytes": removed_bytes,
        "removed": sorted(path.name for path in removed),
    }


def create_container_backup(
    *,
    group: Path,
    backup_dir: Path,
    output: Path | None = None,
    policy: RetentionPolicy = CONTAINER_POLICY,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    group = group.expanduser().resolve()
    if not group.is_dir():
        raise FileNotFoundError("Reminders group container does not exist")
    _ensure_private_dir(backup_dir)
    managed_output = output is None
    out = (
        _available_path(
            backup_dir / f"{CONTAINER_PREFIX}{_timestamp(now)}.tgz"
        )
        if output is None
        else output.expanduser().resolve()
    )
    if out == group or group in out.parents:
        raise ValueError("backup output must be outside the Reminders group container")
    _ensure_private_dir(out.parent, tighten_existing=False)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{out.name}.", suffix=".tmp", dir=out.parent, delete=False
    )
    temp_path = Path(handle.name)
    handle.close()
    try:
        with tarfile.open(temp_path, "w:gz") as archive:
            archive.add(group, arcname="Container_v1")
        temp_path.chmod(0o600)
        os.replace(temp_path, out)
        out.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)

    retention = (
        prune_managed_backups(
            backup_dir=backup_dir,
            kind="container",
            policy=policy,
            protected={out},
        )
        if managed_output
        else None
    )
    return {
        "backup": str(out),
        "bytes": out.stat().st_size,
        "kind": "container_archive",
        "source_scope": "reminders_container",
        "consistency": "best_effort_live_container",
        "retention": retention,
        "warning": (
            "Reminders may write while this archive is created; verify the backup before "
            "relying on it for recovery."
        ),
    }


def create_sqlite_backup(
    *,
    database: Path,
    backup_dir: Path,
    label: str,
    policy: RetentionPolicy = DATABASE_POLICY,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    database = database.expanduser().resolve()
    if not database.is_file():
        raise FileNotFoundError("Reminders database does not exist")
    _ensure_private_dir(backup_dir)
    out = _available_path(
        backup_dir
        / f"{DATABASE_PREFIX}{_safe_label(label)}-{_timestamp(now)}.sqlite"
    )
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{out.name}.", suffix=".tmp", dir=out.parent, delete=False
    )
    temp_path = Path(handle.name)
    handle.close()
    try:
        source_uri = f"file:{database.as_posix()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True)) as source, closing(
            sqlite3.connect(temp_path)
        ) as destination:
            source.backup(destination)
            quick_check = destination.execute("pragma quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise sqlite3.DatabaseError("SQLite backup quick_check failed")
        temp_path.chmod(0o600)
        os.replace(temp_path, out)
        out.chmod(0o600)
    finally:
        temp_path.unlink(missing_ok=True)

    retention = prune_managed_backups(
        backup_dir=backup_dir,
        kind="database",
        policy=policy,
        protected={out},
    )
    return {
        "backup": str(out),
        "bytes": out.stat().st_size,
        "kind": "sqlite_online",
        "source_scope": "single_database",
        "consistency": "sqlite_online_backup",
        "retention": retention,
        "warning": None,
    }
