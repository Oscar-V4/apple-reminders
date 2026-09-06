from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".github/workflows/prepare-signed-native-helper-source.yml"


def run_blocks(workflow):
    blocks = []
    lines = workflow.splitlines()
    for index, line in enumerate(lines):
        if line == "        run: |":
            body = []
            for candidate in lines[index + 1:]:
                if candidate.strip() and not candidate.startswith("          "):
                    break
                body.append(candidate[10:])
            blocks.append("\n".join(body))
    return blocks


class NativeSourceSigningWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = PATH.read_text()
        cls.build = cls.workflow.split("  build-unsigned:", 1)[1].split("  sign-prebuilt:", 1)[0]
        cls.sign = cls.workflow.split("  sign-prebuilt:", 1)[1].split("  attest-and-publish:", 1)[0]
        cls.attest = cls.workflow.split("  attest-and-publish:", 1)[1]

    def test_owner_and_main_gates_apply_to_all_three_jobs(self):
        for job in (self.build, self.sign, self.attest):
            for gate in ("github.repository == 'Oscar-V4/apple-reminders'",
                         "github.actor == 'Oscar-V4'", "github.triggering_actor == 'Oscar-V4'",
                         "github.ref_type == 'branch'", "github.ref_name == github.event.repository.default_branch"):
                self.assertIn(gate, job)

    def test_signing_runs_only_main_owned_tooling_and_reads_source_as_data(self):
        self.assertIn("ref: ${{ github.sha }}", self.sign)
        self.assertNotIn("ref: ${{ inputs.source_commit }}", self.sign)
        self.assertIn('git show "$SOURCE_COMMIT:plugins/apple-reminders/$path"', self.sign)
        for script in ("native_helper_artifact.py", "build_native_helper_app.py",
                       "verify_native_helper.py", "native_helper_app_info.plist",
                       "prepare_signed_native_helper.sh"):
            self.assertIn(script, self.sign.split("# Never check out", 1)[0])
        self.assertIn('git merge-base --is-ancestor "$WORKFLOW_COMMIT" "$SOURCE_COMMIT"', self.sign)
        self.assertNotIn("python3 -m unittest", self.sign)
        self.assertNotIn("python3 scripts/build_native_helper_app.py", self.sign)
        self.assertNotIn("--run-protocol-probes", self.workflow)
        self.assertNotRegex(self.sign, r'(?m)^\s+"\$app(?:/|\")')
        self.assertIn('git check-ref-format "$SOURCE_REF"', self.build)
        self.assertIn('"$(git rev-parse refs/remotes/origin/signed-helper-source)" == "$SOURCE_COMMIT"', self.sign)

    def test_credentials_are_scoped_and_cleanup_precedes_upload(self):
        self.assertNotIn("secrets.", self.build)
        self.assertNotIn("secrets.", self.attest)
        self.assertIn("name: release-signing", self.sign)
        verify = self.sign.index("Validate unsigned artifact before credentials")
        secrets = self.sign.index("secrets.APPLE_DEVELOPER_ID_P12_B64")
        cleanup = self.sign.index("Confirm cleanup before artifact upload")
        upload = self.sign.index("uses: actions/upload-artifact@")
        self.assertLess(verify, secrets)
        self.assertLess(cleanup, upload)
        self.assertIn("trap cleanup EXIT", self.sign)
        self.assertIn('security delete-keychain "$keychain"', self.sign)
        self.assertIn('rm -rf "$credentials"', self.sign)
        self.assertNotIn("set -x", self.workflow)

    def test_attestation_rehashes_signing_outputs_without_checkout(self):
        self.assertNotIn("actions/checkout@", self.attest)
        self.assertNotIn("scripts/", self.attest)
        for job in (self.build, self.sign):
            self.assertNotIn("id-token: write", job)
            self.assertNotIn("attestations: write", job)
        self.assertIn("id-token: write", self.attest)
        self.assertIn("attestations: write", self.attest)
        self.assertIn("needs.sign-prebuilt.outputs.zip_sha256", self.attest)
        self.assertIn("attestation subject inventory drift", self.attest)
        self.assertIn("attestation subject digest drift", self.attest)
        self.assertIn('manifest["workflow_commit"]', self.attest)
        self.assertLess(self.attest.index("Rehash exact subjects"), self.attest.index("uses: actions/attest@"))

    def test_all_actions_are_pinned_and_artifacts_are_run_scoped(self):
        for ref in re.findall(r"uses:\s*(\S+)", self.workflow):
            self.assertRegex(ref, r"^[^@]+@[0-9a-f]{40}$")
        for name in re.findall(r"(?m)^          name: (.*)$", self.workflow):
            self.assertIn("${{ inputs.source_commit }}-${{ github.run_id }}-${{ github.run_attempt }}", name)
        self.assertIn("/.github/workflows/prepare-signed-native-helper-source.yml @Oscar-V4", (ROOT / ".github/CODEOWNERS").read_text())

    def test_embedded_shell_and_python_syntax(self):
        blocks = run_blocks(self.workflow)
        self.assertGreaterEqual(len(blocks), 7)
        for block in blocks:
            # GitHub expressions become ordinary scalar data before the shell.
            shell = re.sub(r"\$\{\{.*?\}\}", "fixture", block)
            result = subprocess.run(["/bin/bash", "-n"], input=shell, text=True,
                                    capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            for code in re.findall(r"<<'PYCODE'\n(.*?)\nPYCODE", block, re.DOTALL):
                ast.parse(code)


if __name__ == "__main__":
    unittest.main()
