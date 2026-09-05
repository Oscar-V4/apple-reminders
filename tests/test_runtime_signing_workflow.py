from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/prepare-signed-runtime-source.yml"
APP = "AppleRemindersPythonRuntime.app"
SOURCE = "1" * 40
TRUSTED = "2" * 40
MACHO_PATHS = [
    "Contents/MacOS/apple-reminders-python",
    "Contents/Resources/python/bin/python3.13",
    "Contents/Resources/python/lib/itcl4.3.8/libitcl4.3.8.dylib",
    "Contents/Resources/python/lib/itcl4.3.8/libtcl9itcl4.3.8.dylib",
    "Contents/Resources/python/lib/libpython3.13.dylib",
    "Contents/Resources/python/lib/libtcl9.0.dylib",
    "Contents/Resources/python/lib/libtcl9tk9.0.dylib",
    "Contents/Resources/python/lib/python3.13/lib-dynload/_dbm.cpython-313-darwin.so",
    "Contents/Resources/python/lib/python3.13/lib-dynload/_tkinter.cpython-313-darwin.so",
    "Contents/Resources/python/lib/thread3.0.6/libtcl9thread3.0.6.dylib",
    "Contents/Resources/python/lib/thread3.0.6/libthread3.0.6.dylib",
]
INPUTS = {
    "scripts/build_python_runtime.py",
    "scripts/python_runtime_launcher.c",
    "scripts/python_runtime_info.plist",
    "scripts/python-runtime-lock.json",
    ".github/workflows/prepare-signed-runtime-source.yml",
}


def python_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    result = []
    index = 0
    while index < len(lines):
        if re.search(r"<<'PY'\s*$", lines[index]):
            index += 1
            start = index
            while index < len(lines) and lines[index].strip() != "PY":
                index += 1
            if index == len(lines):
                raise AssertionError("unterminated inline Python")
            result.append(textwrap.dedent("\n".join(lines[start:index])) + "\n")
        index += 1
    return result


def shell_blocks(text: str) -> list[str]:
    lines = text.splitlines()
    result = []
    index = 0
    while index < len(lines):
        if lines[index] == "        run: |":
            start = index + 1
            index = start
            while index < len(lines) and (not lines[index].strip() or lines[index].startswith("          ")):
                index += 1
            result.append(textwrap.dedent("\n".join(lines[start:index])))
        else:
            index += 1
    return result


class RuntimeWorkflowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = WORKFLOW.read_text()
        names = ["test-and-build-unsigned-source", "sign-notarize-and-staple", "verify-candidates", "attest-and-publish-candidate"]
        starts = [cls.text.index(f"  {name}:") for name in names] + [len(cls.text)]
        cls.jobs = [cls.text[starts[i]:starts[i + 1]] for i in range(4)]

    def test_four_job_permission_separation(self) -> None:
        build, sign, verify, attest = self.jobs
        self.assertIn("environment:\n      name: release-signing", sign)
        self.assertIn("secrets.APPLE_DEVELOPER_ID_P12_B64", sign)
        for job in (build, verify, attest):
            self.assertNotIn("secrets.", job)
        for job in (sign, attest):
            for forbidden in ("actions/checkout@", "actions/setup-python@", "python3 scripts/", "--run-probes"):
                self.assertNotIn(forbidden, job)
        for job in (build, sign, verify):
            self.assertNotIn("id-token: write", job)
            self.assertNotIn("attestations: write", job)
        self.assertIn("id-token: write", attest)
        self.assertIn("attestations: write", attest)
        self.assertNotIn("contents: write", self.text)

    def test_main_owned_source_and_actor_gates(self) -> None:
        for job in self.jobs:
            self.assertIn("github.actor == 'Oscar-V4'", job)
            self.assertIn("github.triggering_actor == 'Oscar-V4'", job)
            self.assertIn("github.ref_type == 'branch'", job)
            self.assertIn("github.ref_name == github.event.repository.default_branch", job)
        build = self.jobs[0]
        for requirement in (
            'git check-ref-format "$SOURCE_REF"',
            '[[ "$(git rev-parse refs/remotes/origin/runtime-source)" == "$SOURCE_COMMIT" ]]',
            '[[ "$(git rev-parse refs/remotes/origin/runtime-workflow-main)" == "$WORKFLOW_COMMIT" ]]',
            'git merge-base --is-ancestor "$WORKFLOW_COMMIT" "$SOURCE_COMMIT"',
            'git diff --exit-code "$WORKFLOW_COMMIT" "$SOURCE_COMMIT" --',
            ".github/workflows/prepare-signed-runtime-source.yml",
        ):
            self.assertIn(requirement, build)

    def test_native_architecture_jobs_finish_before_attestation(self) -> None:
        verify, attest = self.jobs[2:]
        self.assertIn("runner: macos-15\n", verify)
        self.assertIn("runner: macos-15-intel\n", verify)
        self.assertIn('[[ "$(uname -m)" == "$ARCHITECTURE" ]]', verify)
        self.assertIn("--require-developer-id --require-notarized --run-probes", verify)
        self.assertIn("--plugin-root plugins/apple-reminders", verify)
        self.assertIn("- verify-candidates", attest)
        self.assertIn("EXPECTED_VERIFIED_CHECKSUMS_SHA256", attest)
        self.assertNotIn("softwareupdate", self.text)
        self.assertNotIn('rm -rf "$PLUGIN_ROOT/native"', self.text)

    def test_signing_is_nested_first_and_cleanup_precedes_export(self) -> None:
        sign = self.jobs[1]
        nested = sign.index('codesign --force --options runtime --timestamp --sign "$identity" "$app/$relative"')
        outer = sign.index('codesign --force --options runtime --timestamp --sign "$identity" "$app")')
        self.assertLess(nested, outer)
        self.assertNotIn("codesign --deep --force", sign)
        self.assertIn("trap cleanup EXIT", sign)
        cleanup = sign.index("\n          cleanup\n          set -e\n")
        exported = sign.index("signed_checksums_sha256=%s", cleanup)
        upload = sign.index("uses: actions/upload-artifact@", exported)
        self.assertLess(cleanup, exported)
        self.assertLess(exported, upload)
        self.assertIn('code_directory_hash=cdhash[0]', sign)
        self.assertIn('manifest.pop("unsigned_archive_sha256")', sign)
        self.assertIn('manifest.pop("unsigned_archive_bytes")', sign)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS code-signing tools")
    def test_nested_signing_creates_public_resource_modes_without_changing_private_umask(self) -> None:
        selected = subprocess.run(["/usr/bin/xcode-select", "-p"], capture_output=True, timeout=10)
        if selected.returncode:
            self.skipTest("requires an already selected developer toolchain")
        with tempfile.TemporaryDirectory(prefix="runtime-signing-mode-") as temporary:
            app = Path(temporary).resolve() / "mode fixture.app"
            binary = app / "Contents/MacOS/apple-reminders-python"
            binary.parent.mkdir(parents=True, mode=0o755)
            for directory in (app, app / "Contents", binary.parent):
                directory.chmod(0o755)
            (app / "Contents/Info.plist").write_bytes(plistlib.dumps({
                "CFBundleIdentifier": "io.github.oscar-v4.apple-reminders.mode-fixture",
                "CFBundleExecutable": binary.name, "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0",
            }))
            (app / "Contents/Info.plist").chmod(0o644)
            compiled = subprocess.run(
                ["/usr/bin/xcrun", "clang", "-x", "c", "-", "-o", str(binary)],
                input="int main(void) { return 0; }\n", capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            binary.chmod(0o755)
            sign = self.jobs[1]
            nested = next(line.strip() for line in sign.splitlines() if 'codesign --force' in line and '"$app/$relative"' in line)
            outer = next(line.strip() for line in sign.splitlines() if 'codesign --force' in line and '"$app"' in line)
            # Exercise the real workflow shell commands with an ad-hoc identity
            # and no timestamp service. The fixture executable is never run.
            script = (
                "set -euo pipefail\numask 077\nidentity=-\n"
                f"app={shlex.quote(str(app))}\nrelative=Contents/MacOS/apple-reminders-python\n"
                + nested.replace("--timestamp ", "--timestamp=none ") + "\n"
                + outer.replace("--timestamp ", "--timestamp=none ") + "\n"
                + '/usr/bin/codesign --verify --deep --strict "$app"\n'
                + "printf 'caller_umask=%s\\n' \"$(umask)\"\n"
            )
            result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("caller_umask=0077", result.stdout)
            resource = app / "Contents/_CodeSignature/CodeResources"
            self.assertEqual(stat.S_IMODE(resource.stat().st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(resource.parent.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)

    def test_shell_and_embedded_python_compile(self) -> None:
        blocks = python_blocks(self.text)
        self.assertEqual(len(blocks), 5)
        for index, source in enumerate(blocks):
            compile(source, f"{WORKFLOW}:inline-{index}", "exec")
        runs = shell_blocks(self.text)
        self.assertEqual(len(runs), 5)
        for source in runs:
            result = subprocess.run(["/bin/bash", "-n"], input=source, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_native_unsigned_validation_runs_under_bash_nounset(self) -> None:
        build = self.jobs[0]
        start = build.index("          for architecture in arm64 x86_64; do\n")
        end = build.index("          done\n", start) + len("          done\n")
        loop = textwrap.dedent(build[start:end])
        script = """set -euo pipefail
RUNNER_TEMP=/synthetic-runtime
artifacts=/synthetic-artifacts
SOURCE_COMMIT=1111111111111111111111111111111111111111
WORKFLOW_COMMIT=2222222222222222222222222222222222222222
uname() { printf 'arm64\\n'; }
python3() { printf '<%s>' "$@"; printf '\\n'; }
""" + loop
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = result.stdout.splitlines()
        self.assertEqual(len(calls), 4)
        self.assertIn("<--run-probes>", calls[1])
        self.assertIn("<scripts/verify_python_runtime.py>", calls[3])
        self.assertIn("<--architecture><x86_64>", calls[3])
        self.assertNotIn("<--run-probes>", calls[3])

    def test_actions_pinned_and_runtime_files_codeowned(self) -> None:
        actions = re.findall(r"(?m)^\s*-\s*uses:\s*([^\s#]+)", self.text)
        self.assertGreaterEqual(len(actions), 10)
        for action in actions:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")
        codeowners = (ROOT / ".github/CODEOWNERS").read_text()
        for path in (".github/workflows/prepare-signed-runtime-source.yml", "plugins/apple-reminders/runtime/**", *INPUTS):
            self.assertIn(f"/{path} @Oscar-V4", codeowners)


class RuntimeSigningAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="runtime-signing-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.guard = python_blocks(WORKFLOW.read_text())[0]
        self.manifests = {}
        self.contents = {name: b"\xcf\xfa\xed\xfe" + b"synthetic native fixture" for name in MACHO_PATHS}
        self.contents["Contents/Info.plist"] = b"synthetic plist fixture"
        lock = json.loads((ROOT / "scripts/python-runtime-lock.json").read_text())
        for architecture in ("arm64", "x86_64"):
            self.manifests[architecture] = {
                "schema_version": 1, "architecture": architecture,
                "target_triple": lock["architectures"][architecture]["target_triple"],
                "runtime_version": lock["python_version"], "runtime_build": lock["release"],
                "app_name": APP, "bundle_identifier": "io.github.oscar-v4.apple-reminders.python-runtime",
                "executable_relative_path": "Contents/MacOS/apple-reminders-python",
                "source_commit": SOURCE, "workflow_commit": TRUSTED,
                "source_input_sha256": {name: "a" * 64 for name in INPUTS},
                "upstream": {kind: lock["architectures"][architecture][kind] for kind in ("install_only_stripped", "full")},
                "normalization": {"omitted_symlinks": ["python/bin/python", "python/bin/python3"], "dereferenced_symlinks": []},
            }
        self.rebuild()

    def rebuild(self, *, extra_member: tuple[str, int, bytes] | None = None) -> None:
        for architecture, manifest in self.manifests.items():
            directories = {str(parent) for name in self.contents for parent in PurePosixPath(name).parents if str(parent) != "."}
            manifest["directories"] = [{"path": name, "mode": 0o755} for name in sorted(directories)]
            manifest["files"] = [
                {"path": name, "mode": 0o755 if name in MACHO_PATHS else 0o644, "bytes": len(data),
                 "sha256": hashlib.sha256(data).hexdigest(), "mach_o": name in MACHO_PATHS}
                for name, data in sorted(self.contents.items())
            ]
            manifest["macho_paths"] = [entry["path"] for entry in manifest["files"] if entry["mach_o"]]
            archive = self.artifacts / f"python-runtime-macos-{architecture}.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                entries = [(APP + "/", stat.S_IFDIR | 0o755, b"")]
                entries += [(APP + "/" + name + "/", stat.S_IFDIR | 0o755, b"") for name in sorted(directories)]
                entries += [(APP + "/" + entry["path"], stat.S_IFREG | entry["mode"], self.contents[entry["path"]]) for entry in manifest["files"]]
                if extra_member:
                    entries.append(extra_member)
                for name, mode, data in entries:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    handle.writestr(info, data)
            manifest["unsigned_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest["unsigned_archive_bytes"] = archive.stat().st_size
        self.write_manifests()

    def write_manifests(self) -> None:
        for architecture, manifest in self.manifests.items():
            (self.artifacts / f"python-runtime-build-{architecture}.json").write_text(json.dumps(manifest))
        names = sorted(path.name for path in self.artifacts.iterdir() if path.name != "SHA256SUMS")
        self.checksums = "".join(f"{hashlib.sha256((self.artifacts / name).read_bytes()).hexdigest()}  {name}\n" for name in names)
        (self.artifacts / "SHA256SUMS").write_text(self.checksums)
        self.expected_digest = hashlib.sha256(self.checksums.encode()).hexdigest()

    def run_guard(self, expected: str | None = None) -> None:
        result = subprocess.run(
            [sys.executable, "-c", self.guard, str(self.artifacts), str(self.work)],
            env={**os.environ, "SOURCE_COMMIT": SOURCE, "WORKFLOW_COMMIT": TRUSTED,
                 "EXPECTED_UNSIGNED_CHECKSUMS_SHA256": self.expected_digest},
            capture_output=True, text=True,
        )
        if expected is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)

    def test_valid_two_architecture_inventory_and_percent_encoded_upstream_urls(self) -> None:
        self.run_guard()
        self.assertEqual((self.work / "macho-arm64.txt").read_text().splitlines()[-1], "Contents/MacOS/apple-reminders-python")
        self.assertEqual((self.work / "arm64" / APP / MACHO_PATHS[0]).read_bytes(), self.contents[MACHO_PATHS[0]])

    def test_extra_member_rejected_before_extraction(self) -> None:
        self.rebuild(extra_member=("unexpected.txt", stat.S_IFREG | 0o644, b"fixture"))
        self.run_guard("unsigned ZIP member inventory drift")
        self.assertFalse((self.work / "arm64" / APP).exists())

    def test_unsafe_manifest_path_is_rejected(self) -> None:
        self.manifests["arm64"]["files"][0]["path"] = "../escape"
        self.write_manifests()
        self.run_guard("unsafe runtime path")
        self.assertFalse((self.root / "escape").exists())

    def test_symlink_artifact_is_rejected(self) -> None:
        target = self.artifacts / "python-runtime-macos-arm64.zip"
        replacement = self.root / "other.zip"
        target.rename(replacement)
        target.symlink_to(replacement)
        self.run_guard("unsigned artifact is not a regular file")

    def test_symlink_member_with_an_allowed_name_is_rejected(self) -> None:
        archive = self.artifacts / "python-runtime-macos-arm64.zip"
        with zipfile.ZipFile(archive) as handle:
            members = [(entry, handle.read(entry)) for entry in handle.infolist()]
        with zipfile.ZipFile(archive, "w") as handle:
            for entry, data in members:
                if entry.filename == APP + "/Contents/Info.plist":
                    entry.external_attr = (stat.S_IFLNK | 0o644) << 16
                handle.writestr(entry, data)
        self.manifests["arm64"]["unsigned_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.manifests["arm64"]["unsigned_archive_bytes"] = archive.stat().st_size
        self.write_manifests()
        self.run_guard("unsigned ZIP mode, size or encryption drift")

    def test_directory_outside_the_runtime_tree_is_rejected(self) -> None:
        self.manifests["arm64"]["directories"].append({"path": "Unexpected", "mode": 0o755})
        self.write_manifests()
        self.run_guard("runtime directory outside allowed app tree")

    def test_duplicate_manifest_member_is_rejected(self) -> None:
        self.manifests["arm64"]["files"].append(self.manifests["arm64"]["files"][0])
        self.write_manifests()
        self.run_guard("duplicate runtime member")

    def test_unknown_native_code_is_rejected(self) -> None:
        self.contents["Contents/Resources/python/lib/extra.dylib"] = b"\xcf\xfa\xed\xfe" + b"extra fixture"
        self.rebuild()
        self.run_guard("runtime native-code inventory drift")

    def test_false_native_type_claim_is_rejected(self) -> None:
        self.manifests["arm64"]["files"][1]["mach_o"] = False
        self.write_manifests()
        self.run_guard("runtime native-code inventory drift")

    def test_source_commit_mismatch_is_rejected(self) -> None:
        self.manifests["arm64"]["source_commit"] = "3" * 40
        self.write_manifests()
        self.run_guard("unsigned manifest identity drift")

    def test_changed_source_manifest_cannot_rewrite_trusted_checksums(self) -> None:
        (self.artifacts / "python-runtime-build-arm64.json").write_text("{}")
        self.run_guard("unsigned checksum inventory drift")

    def test_oversized_expanded_member_is_rejected(self) -> None:
        self.manifests["arm64"]["files"][0]["bytes"] = 51 * 1024 * 1024
        self.write_manifests()
        self.run_guard("runtime file mode or size drift")

    def test_unsigned_source_input_inventory_is_closed(self) -> None:
        self.manifests["arm64"]["source_input_sha256"]["unexpected.py"] = "b" * 64
        self.write_manifests()
        self.run_guard("unsigned source input inventory drift")


class RuntimeSignedInventoryDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="runtime-mode-diagnostic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.unsigned, self.work, self.signed = [self.root / name for name in ("unsigned", "work", "signed")]
        for directory in (self.unsigned, self.work, self.signed):
            directory.mkdir()
        self.app = self.work / "arm64" / APP
        (self.app / "Contents/_CodeSignature").mkdir(parents=True)
        for directory in (self.app, self.app / "Contents", self.app / "Contents/_CodeSignature"):
            directory.chmod(0o755)
        (self.unsigned / "python-runtime-build-arm64.json").write_text(json.dumps({
            "files": [], "directories": [{"path": "Contents", "mode": 0o755}],
        }))
        self.writer = python_blocks(WORKFLOW.read_text())[2]

    def run_writer(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", self.writer, str(self.unsigned), str(self.work), str(self.signed)],
            capture_output=True, text=True,
        )

    def test_known_signature_resource_mode_reports_relative_path_and_modes(self) -> None:
        path = self.app / "Contents/_CodeSignature/CodeResources"
        path.write_bytes(b"synthetic signature resource")
        path.chmod(0o600)
        result = self.run_writer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Contents/_CodeSignature/CodeResources: got 0600, expected 0644", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)

    def test_known_directory_mode_reports_relative_path_and_modes(self) -> None:
        (self.app / "Contents/_CodeSignature").chmod(0o700)
        result = self.run_writer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Contents/_CodeSignature: got 0700, expected 0755", result.stderr)
        self.assertNotIn(str(self.root), result.stderr)

    def test_unrecognized_path_is_rejected_without_echoing_its_name(self) -> None:
        (self.app / "Contents/unrecognized-synthetic-name").write_bytes(b"synthetic extra file")
        result = self.run_writer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("signed runtime file inventory drift", result.stderr)
        self.assertNotIn("unrecognized-synthetic-name", result.stderr)


class RuntimeFinalAttestationBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="runtime-attestation-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.guard = python_blocks(WORKFLOW.read_text())[-1]
        self.names = []
        for architecture in ("arm64", "x86_64"):
            archive = self.root / f"python-runtime-macos-{architecture}.zip"
            archive.write_bytes(b"opaque synthetic signed artifact")
            manifest = self.root / f"python-runtime-build-{architecture}.json"
            manifest.write_text(json.dumps({
                "source_commit": SOURCE, "workflow_commit": TRUSTED, "architecture": architecture,
                "signature": "developer-id", "team_id": "V8347N9346", "notarized": True,
                "notarization_checked": True, "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }))
            self.names += [archive.name, manifest.name]
        self.write_checksums()

    def write_checksums(self) -> None:
        path = self.root / "SHA256SUMS"
        path.write_text("".join(f"{hashlib.sha256((self.root / name).read_bytes()).hexdigest()}  {name}\n" for name in sorted(self.names)))
        self.expected_digest = hashlib.sha256(path.read_bytes()).hexdigest()

    def run_guard(self, expected: str | None = None) -> None:
        result = subprocess.run(
            [sys.executable, "-c", self.guard, str(self.root)],
            env={**os.environ, "SOURCE_COMMIT": SOURCE, "WORKFLOW_COMMIT": TRUSTED,
                 "EXPECTED_SIGNED_CHECKSUMS_SHA256": self.expected_digest},
            capture_output=True, text=True,
        )
        if expected is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)

    def test_exact_five_subject_inventory_passes(self) -> None:
        self.run_guard()

    def test_extra_subject_is_rejected(self) -> None:
        (self.root / "extra.txt").write_text("synthetic extra")
        self.run_guard("attestation subject inventory drift")

    def test_changed_capsule_after_tests_is_rejected(self) -> None:
        (self.root / "python-runtime-macos-arm64.zip").write_bytes(b"changed bytes")
        self.run_guard("attestation subject digest drift")

    def test_rewritten_checksum_file_cannot_change_protected_identity(self) -> None:
        original = self.expected_digest
        (self.root / "python-runtime-macos-arm64.zip").write_bytes(b"changed bytes")
        self.write_checksums()
        self.expected_digest = original
        self.run_guard("attestation checksum digest drift")

    def test_manifest_workflow_identity_is_checked_again(self) -> None:
        path = self.root / "python-runtime-build-arm64.json"
        manifest = json.loads(path.read_text())
        manifest["workflow_commit"] = "3" * 40
        path.write_text(json.dumps(manifest))
        self.write_checksums()
        self.run_guard("attestation manifest identity drift")


if __name__ == "__main__":
    unittest.main()
