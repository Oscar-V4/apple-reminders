from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import prepare_helper_signing as signing


class HelperSigningPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.calls = []
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        for path in signing.BUILD_INPUTS:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("reviewed synthetic build input\n")
        self.manifest = self.root / "plugins/apple-reminders/.codex-plugin/plugin.json"
        self.manifest.parent.mkdir(parents=True)
        self.manifest.write_text('{"version":"0.7.0"}')
        self.commit("main fixture")
        self.main_commit = self.git("rev-parse", "HEAD").decode().strip()
        self.git("switch", "-c", "release/0.7.1-source")
        self.manifest.write_text('{"version":"0.7.1"}')
        self.commit("source fixture")
        self.source_commit = self.git("rev-parse", "HEAD").decode().strip()
        self.remote = {signing.MAIN_REF: self.main_commit,
                       "refs/heads/release/0.7.1-source": self.source_commit}

    def git(self, *args):
        result = subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=False)
        if result.returncode:
            raise signing.PreflightError("synthetic Git failure")
        return result.stdout

    def commit(self, message):
        self.git("add", ".")
        self.git("commit", "-m", message)

    def command(self, argv, root):
        self.assertEqual(root.resolve(), self.root)
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "ls-remote"]:
            self.assertEqual(argv[5], signing.CANONICAL_GIT_URL)
            wanted = set(argv[6:])
            return "".join(f"{commit}\t{ref}\n" for ref, commit in self.remote.items() if ref in wanted).encode()
        if argv[:3] == ["gh", "workflow", "run"]:
            return b""
        self.assertEqual(argv[0], "git")
        return self.git(*argv[1:])

    def plan(self, ref="release/0.7.1-source"):
        return signing.plan_signing(ref, self.root, command=self.command)

    def mutations(self):
        return [argv for argv in self.calls if argv[0] == "gh"]

    def test_short_branch_emits_canonical_ref_for_both_existing_workflows(self):
        before = self.git("status", "--porcelain")
        plan = self.plan()
        self.assertEqual(plan, self.plan("refs/heads/release/0.7.1-source"))
        self.assertFalse(plan["dispatch_attempted"])
        self.assertEqual(plan["source_ref"], "refs/heads/release/0.7.1-source")
        self.assertEqual(plan["source_commit"], self.source_commit)
        self.assertEqual(plan["workflow_commit"], self.main_commit)
        self.assertEqual(plan["dispatch_argv"], [*plan["preparation_command"],
            "refs/heads/release/0.7.1-source", "--repo-root", str(self.root),
            "--dispatch", "--reviewed-plan-sha256", plan["plan_sha256"]])
        self.assertEqual({item["workflow"] for item in plan["dispatches"]}, set(signing.WORKFLOWS))
        for item in plan["dispatches"]:
            self.assertIn("source_ref=refs/heads/release/0.7.1-source", item["argv"])
            self.assertIn(f"source_commit={self.source_commit}", item["argv"])
            self.assertEqual(item["argv"][item["argv"].index("--ref") + 1], "refs/heads/main")
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(self.mutations(), [])
        self.assertFalse(any(argv[1] in {"fetch", "checkout", "switch"} for argv in self.calls))

    def test_invalid_nonbranch_and_ambiguous_inputs_never_reach_network(self):
        self.git("tag", "release/0.7.1-source")
        for value in ("", "HEAD", "@", "-branch", "refs/tags/v0.7.1", "refs/remotes/origin/main",
                      "release/bad..ref", "release/missing", self.source_commit, "release/0.7.1-source"):
            self.calls.clear()
            with self.subTest(value=value), self.assertRaises(signing.PreflightError):
                self.plan(value)
            self.assertFalse(any(argv[:2] == ["git", "ls-remote"] for argv in self.calls))
            self.assertEqual(self.mutations(), [])
        # Explicit namespace is unambiguous even with a same-named local tag.
        self.plan("refs/heads/release/0.7.1-source")

    def test_object_abbreviation_colliding_with_a_branch_is_ambiguous(self):
        ambiguous = self.main_commit[:8]
        self.git("branch", ambiguous)
        with self.assertRaisesRegex(signing.PreflightError, "Ambiguous short ref"):
            self.plan(ambiguous)
        self.assertFalse(any(argv[:2] == ["git", "ls-remote"] for argv in self.calls))

    def test_main_and_source_must_match_canonical_remote(self):
        for ref in (signing.MAIN_REF, "refs/heads/release/0.7.1-source"):
            original = self.remote[ref]
            self.remote[ref] = "f" * 40
            with self.subTest(ref=ref), self.assertRaisesRegex(signing.PreflightError, "commits differ"):
                self.plan()
            self.remote[ref] = original
        self.assertEqual(self.mutations(), [])

    def test_already_tagged_version_is_rejected_locally_and_remotely(self):
        self.remote["refs/tags/v0.7.1"] = self.source_commit
        with self.assertRaisesRegex(signing.PreflightError, "already tagged on"):
            self.plan()
        del self.remote["refs/tags/v0.7.1"]
        self.git("tag", "v0.7.1")
        self.calls.clear()
        with self.assertRaisesRegex(signing.PreflightError, "already tagged locally"):
            self.plan()
        self.assertFalse(any(argv[:2] == ["git", "ls-remote"] for argv in self.calls))
        self.assertEqual(self.mutations(), [])

    def test_main_owned_input_drift_fails_before_remote_or_dispatch(self):
        (self.root / signing.BUILD_INPUTS[0]).write_text("changed recipe")
        self.commit("unsafe changed recipe")
        with self.assertRaisesRegex(signing.PreflightError, "Main-owned signing input changed"):
            self.plan()
        self.assertFalse(any(argv[:2] == ["git", "ls-remote"] for argv in self.calls))
        self.assertEqual(self.mutations(), [])

    def test_source_must_contain_main_history(self):
        self.git("switch", "main")
        (self.root / "main-only.txt").write_text("new main work")
        self.commit("main advances beyond source")
        with self.assertRaisesRegex(signing.PreflightError, "main must be an ancestor"):
            self.plan()
        self.assertEqual(self.mutations(), [])

    def test_explicit_dispatch_revalidates_and_submits_both_exact_argvs(self):
        plan = self.plan()
        receipt, status = signing.dispatch_reviewed_plan(plan, self.root, command=self.command)
        self.assertEqual(status, 0)
        self.assertEqual(self.mutations(), [item["argv"] for item in plan["dispatches"]])
        self.assertEqual(receipt["dispatch_outcome"], "requests_accepted")
        self.assertEqual(sum(argv[:2] == ["git", "ls-remote"] for argv in self.calls), 3)

    def test_pre_dispatch_remote_change_cannot_mutate_network(self):
        plan = self.plan()
        self.remote[signing.MAIN_REF] = "f" * 40
        receipt, status = signing.dispatch_reviewed_plan(plan, self.root, command=self.command)
        self.assertEqual(status, 1)
        self.assertFalse(receipt["dispatch_attempted"])
        self.assertEqual(self.mutations(), [])

    def test_branch_movement_between_requests_stops_after_first_acceptance(self):
        plan = self.plan()
        def move_main_after_first(argv, root):
            result = self.command(argv, root)
            if argv[:3] == ["gh", "workflow", "run"]:
                self.remote[signing.MAIN_REF] = "f" * 40
            return result
        receipt, status = signing.dispatch_reviewed_plan(plan, self.root, command=move_main_after_first)
        self.assertEqual(status, 1)
        self.assertEqual(receipt["dispatch_outcome"], "stopped_before_next_request")
        self.assertEqual(receipt["dispatch_results"], [
            {"workflow": signing.WORKFLOWS[0], "outcome": "request_accepted"}])
        self.assertEqual(len(self.mutations()), 1)

    def test_second_request_failure_reports_first_acceptance_and_no_retry(self):
        plan = self.plan()
        def fail_second(argv, root):
            if argv[:3] == ["gh", "workflow", "run"] and argv[3] == signing.WORKFLOWS[1]:
                self.calls.append(list(argv))
                raise signing.PreflightError("synthetic uncertain request")
            return self.command(argv, root)
        receipt, status = signing.dispatch_reviewed_plan(plan, self.root, command=fail_second)
        self.assertEqual(status, 1)
        self.assertEqual([item["outcome"] for item in receipt["dispatch_results"]],
                         ["request_accepted", "request_failed_or_unconfirmed"])
        self.assertEqual(len(self.mutations()), 2)

    def test_cli_default_is_plan_only(self):
        plan = self.plan()
        with patch.object(signing, "plan_signing", return_value=plan), \
             patch.object(signing, "dispatch_reviewed_plan", side_effect=AssertionError("implicit dispatch")), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(signing.main(["release/0.7.1-source"]), 0)
        self.assertFalse(json.loads(output.getvalue())["dispatch_attempted"])

    def test_dispatch_requires_an_exact_previously_reviewed_plan(self):
        plan = self.plan()
        for review_hash in (None, "0" * 64):
            argv = ["release/0.7.1-source", "--dispatch"]
            if review_hash:
                argv += ["--reviewed-plan-sha256", review_hash]
            with self.subTest(review_hash=review_hash), \
                 patch.object(signing, "plan_signing", return_value=plan), \
                 patch.object(signing, "dispatch_reviewed_plan", side_effect=AssertionError("unreviewed dispatch")), \
                 redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                signing.main(argv)
        with patch.object(signing, "plan_signing", return_value=plan), \
             patch.object(signing, "dispatch_reviewed_plan", return_value=({"accepted": True}, 0)) as dispatch, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(signing.main(["release/0.7.1-source", "--dispatch",
                "--reviewed-plan-sha256", plan["plan_sha256"]]), 0)
        dispatch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
