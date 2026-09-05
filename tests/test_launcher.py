from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import venv
from pathlib import Path


LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "plugins/apple-reminders/scripts/launch_mcp.sh"
)


class LauncherTests(unittest.TestCase):
    """Exercise the shell launcher with synthetic runtimes and no Reminders access."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="reminders launcher ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugin = self.root / "plugin with spaces"
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "mcp").mkdir()
        (self.plugin / "mcp/server.py").write_text(
            "import json, sys\n"
            "print(json.dumps({'args': sys.argv[1:], "
            "'no_bytecode': sys.dont_write_bytecode}))\n",
            encoding="utf-8",
        )

        # Relocate only the fixed filesystem locations, keeping the discovery,
        # alias resolution, version probe, and exec logic unchanged. This makes
        # missing-runtime tests independent of the developer's installed Python
        # and proves shim avoidance without invoking Apple's real installer.
        self.system_python = self.root / "system/usr/bin/python3"
        self.homebrew = self.root / "homebrew/bin"
        self.intel_homebrew = self.root / "intel-homebrew/bin"
        self.python_org = self.root / "python.org/Versions"
        source = LAUNCHER.read_text(encoding="utf-8")
        substitutions = {
            "/usr/bin/python3": str(self.system_python),
            '|"/bin/python3")': f'|"{self.root}/system/bin/python3")',
            "/opt/homebrew/bin": str(self.homebrew),
            "/usr/local/bin": str(self.intel_homebrew),
            "/Library/Frameworks/Python.framework/Versions": str(self.python_org),
        }
        for original, replacement in substitutions.items():
            self.assertIn(original, source)
            source = source.replace(original, replacement)
        self.launcher = self.plugin / "scripts/launch_mcp.sh"
        self.launcher.write_text(source, encoding="utf-8")

    def executable(self, path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def supported(self, directory: Path) -> Path:
        return self.executable(
            directory / "python3", f"exec {shlex.quote(sys.executable)} \"$@\""
        )

    def shim(self) -> Path:
        self.shim_marker = self.root / "shim-was-executed"
        return self.executable(
            self.system_python,
            f"printf invoked > {shlex.quote(str(self.shim_marker))}\nexit 99",
        )

    def launch(
        self, directories: list[Path] | None = None, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.launcher), *arguments],
            cwd=self.plugin,
            env={"PATH": ":".join(map(str, directories or []))},
            input="",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )

    def assert_success(self, result: subprocess.CompletedProcess[str]) -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"args": [], "no_bytecode": True})
        self.assertEqual(result.stderr, "")

    def test_system_shim_is_never_probed_before_supported_path_python(self) -> None:
        shim = self.shim()
        supported = self.supported(self.root / "user/bin")
        self.assert_success(self.launch([shim.parent, supported.parent]))
        self.assertFalse(self.shim_marker.exists())

    def test_relative_symlink_chain_to_system_shim_is_not_probed(self) -> None:
        self.shim()
        alias = self.root / "aliases/python3"
        alias.parent.mkdir()
        alias.symlink_to("next-python")
        alias.with_name("next-python").symlink_to("../system/usr/bin/python3")
        self.supported(self.homebrew)
        self.assert_success(self.launch([alias.parent]))
        self.assertFalse(self.shim_marker.exists())

    def test_directory_alias_to_system_shim_is_not_probed(self) -> None:
        shim = self.shim()
        alias = self.root / "system-bin-alias"
        alias.symlink_to(shim.parent, target_is_directory=True)
        self.supported(self.homebrew)
        self.assert_success(self.launch([alias]))
        self.assertFalse(self.shim_marker.exists())

    def test_hard_link_to_system_shim_is_not_probed(self) -> None:
        shim = self.shim()
        alias = self.root / "hard-link/python3"
        alias.parent.mkdir()
        os.link(shim, alias)
        self.supported(self.homebrew)
        self.assert_success(self.launch([alias.parent]))
        self.assertFalse(self.shim_marker.exists())

    def test_finder_path_can_find_homebrew_without_developer_tools(self) -> None:
        shim = self.shim()
        self.supported(self.homebrew)
        self.assert_success(self.launch([shim.parent]))
        self.assertFalse(self.shim_marker.exists())

    def test_empty_path_can_find_python_org(self) -> None:
        self.supported(self.python_org / "Current/bin")
        self.assert_success(self.launch())

    def test_intel_homebrew_and_versioned_python_org_are_discovered(self) -> None:
        for directory in (self.intel_homebrew, self.python_org / "3.12/bin"):
            with self.subTest(directory=directory):
                python = self.supported(directory)
                self.assert_success(self.launch())
                python.unlink()

    def test_unsupported_runtime_is_not_used_to_launch_server(self) -> None:
        marker = self.root / "unsupported-invocations"
        old = self.executable(
            self.root / "old/bin/python3",
            f"printf '%s\\n' \"$1\" >> {shlex.quote(str(marker))}\nexit 1",
        )
        result = self.launch([old.parent])
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout, "")
        self.assertEqual(marker.read_text(encoding="utf-8"), "-c\n")
        self.assertIn("https://www.python.org/downloads/macos/", result.stderr)
        self.assertIn("Xcode is not required", result.stderr)

    def test_only_system_shim_reports_python_install_without_invoking_it(self) -> None:
        shim = self.shim()
        result = self.launch([shim.parent])
        self.assertEqual(result.returncode, 78)
        self.assertEqual(result.stdout, "")
        self.assertIn("Python 3.11 or newer", result.stderr)
        self.assertFalse(self.shim_marker.exists())

    def test_broken_and_cyclic_links_do_not_block_later_supported_python(self) -> None:
        for name, destination in (("broken", "missing"), ("cycle", "python3")):
            with self.subTest(name=name):
                alias = self.root / name / "python3"
                alias.parent.mkdir()
                alias.symlink_to(destination)
                self.supported(self.homebrew)
                self.assert_success(self.launch([alias.parent]))

    def test_options_are_forwarded_exactly(self) -> None:
        self.supported(self.homebrew)
        arguments = ["--experimental", "value with spaces", "$literal"]
        result = self.launch([], *arguments)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["args"], arguments)

    def test_supported_symlink_preserves_python_environment_identity(self) -> None:
        # Resolving an allowed venv's Python for exec would silently leave its
        # environment. Resolve aliases only to decide whether to reject a shim.
        python = self.root / "venv/bin/python3"
        venv.EnvBuilder(with_pip=False, symlinks=True).create(python.parent.parent)
        (self.plugin / "mcp/server.py").write_text(
            "import sys\nprint(sys.executable)\n", encoding="utf-8"
        )
        result = self.launch([python.parent])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(python))

    def test_shell_syntax(self) -> None:
        result = subprocess.run(
            ["/bin/sh", "-n", str(LAUNCHER)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
