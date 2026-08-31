from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_ROOT = ROOT / ".github" / "workflows"
SOURCE_SIGNING_WORKFLOW = WORKFLOW_ROOT / "prepare-signed-helper-source.yml"
UPLOAD_ARTIFACT_NODE24 = (
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
)
DOWNLOAD_ARTIFACT_NODE24 = (
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"
)
UPLOAD_ARTIFACT_PROVENANCE_BOUND = (
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
)
DOWNLOAD_ARTIFACT_PROVENANCE_BOUND = (
    "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
)


def workflow_texts() -> dict[Path, str]:
    return {
        path: path.read_text(encoding="utf-8")
        for pattern in ("*.yml", "*.yaml")
        for path in sorted(WORKFLOW_ROOT.glob(pattern))
    }


def artifact_refs(text: str, action: str) -> list[str]:
    return [
        reference
        for reference in re.findall(
            r"(?m)^\s*(?:-\s*)?uses:\s*([^\s#]+)",
            text,
        )
        if reference.startswith(f"actions/{action}@")
    ]


class JavaScriptActionRuntimePinTests(unittest.TestCase):
    def test_safe_workflows_use_node24_with_one_exact_provenance_exception(self) -> None:
        texts = workflow_texts()
        source_text = texts.pop(SOURCE_SIGNING_WORKFLOW)
        other_text = "\n".join(texts.values())

        self.assertEqual(
            artifact_refs(source_text, "upload-artifact"),
            [UPLOAD_ARTIFACT_PROVENANCE_BOUND] * 4,
        )
        self.assertEqual(
            artifact_refs(source_text, "download-artifact"),
            [DOWNLOAD_ARTIFACT_PROVENANCE_BOUND] * 3,
        )
        self.assertEqual(
            artifact_refs(other_text, "upload-artifact"),
            [UPLOAD_ARTIFACT_NODE24] * 4,
        )
        self.assertEqual(
            artifact_refs(other_text, "download-artifact"),
            [DOWNLOAD_ARTIFACT_NODE24] * 4,
        )

    def test_deprecated_node20_pins_cannot_escape_the_bound_workflow(self) -> None:
        texts = workflow_texts()
        source_text = texts.pop(SOURCE_SIGNING_WORKFLOW)
        other_text = "\n".join(texts.values())

        self.assertEqual(source_text.count(UPLOAD_ARTIFACT_PROVENANCE_BOUND), 4)
        self.assertEqual(source_text.count(DOWNLOAD_ARTIFACT_PROVENANCE_BOUND), 3)
        self.assertNotIn(UPLOAD_ARTIFACT_PROVENANCE_BOUND, other_text)
        self.assertNotIn(DOWNLOAD_ARTIFACT_PROVENANCE_BOUND, other_text)
        self.assertEqual(other_text.count("# v7.0.1 (Node 24)"), 4)
        self.assertEqual(other_text.count("# v8.0.1 (Node 24)"), 4)


if __name__ == "__main__":
    unittest.main()
