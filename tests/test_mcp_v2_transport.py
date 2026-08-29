from __future__ import annotations

import unittest
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
sys.path.insert(0, str(PLUGIN_ROOT))

from mcp.v2_transport import DispatchCertainty, TransportResult


class TransportResultTests(unittest.TestCase):
    def test_only_error_results_can_prove_dispatch_never_started(self) -> None:
        result = TransportResult(
            payload={"ok": False},
            is_error=True,
            dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
        )

        self.assertTrue(result.proves_not_started)

        with self.assertRaisesRegex(ValueError, "cannot be undispatched"):
            TransportResult(
                payload={"ok": True},
                is_error=False,
                dispatch_certainty=DispatchCertainty.PROVEN_NOT_STARTED,
            )

    def test_child_success_is_independent_from_dispatch_certainty(self) -> None:
        result = TransportResult(
            payload={"ok": True},
            is_error=False,
            dispatch_certainty=DispatchCertainty.MAY_HAVE_STARTED,
        )

        self.assertFalse(result.proves_not_started)
        self.assertFalse(result.is_error)


if __name__ == "__main__":
    unittest.main()
