#!/usr/bin/env python3
"""Build a byte-for-byte deterministic, allowlisted Codex plugin ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from audit_source_package import FIXED_ZIP_TIMESTAMP, audit_archive, audit_source


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"


def build_package(root: Path, output_directory: Path, *, force: bool = False) -> Path:
    root = root.expanduser().resolve()
    output_directory = output_directory.expanduser().resolve()
    audit = audit_source(root)
    if audit.errors:
        raise RuntimeError("source package audit failed: " + "; ".join(audit.errors))
    manifest = json.loads((root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    plugin_name = manifest["name"]
    archive = output_directory / f"{plugin_name}-{manifest['version']}.zip"
    output_directory.mkdir(parents=True, exist_ok=True)
    if archive.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing archive: {archive}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{plugin_name}-", suffix=".zip.tmp", dir=output_directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, mode="w", compression=zipfile.ZIP_STORED) as handle:
            for relative in audit.files:
                member = f"{plugin_name}/{relative.as_posix()}"
                info = zipfile.ZipInfo(member, date_time=FIXED_ZIP_TIMESTAMP)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_STORED
                info.external_attr = (0o100644 & 0xFFFF) << 16
                handle.writestr(info, (root / relative).read_bytes())
        if archive.exists():
            archive.unlink()
        os.replace(temporary, archive)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    errors = audit_archive(root, archive)
    if errors:
        archive.unlink(missing_ok=True)
        raise RuntimeError("built archive failed audit: " + "; ".join(errors))
    return archive


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin", nargs="?", type=Path, default=PLUGIN_ROOT)
    parser.add_argument(
        "--output-directory",
        type=Path,
        default=REPO_ROOT / "dist",
    )
    parser.add_argument("--force", action="store_true", help="Replace only the exact versioned artifact")
    args = parser.parse_args(argv)
    try:
        archive = build_package(args.plugin, args.output_directory, force=args.force)
    except (FileExistsError, OSError, RuntimeError) as exc:
        parser.exit(1, f"source package build failed: {exc}\n")
    print(
        json.dumps(
            {
                "archive": str(archive),
                "bytes": archive.stat().st_size,
                "sha256": sha256(archive),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
