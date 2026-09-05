from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "plugins" / "apple-reminders" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from reminders_service import (  # noqa: E402
    MoveToListAction,
    PatchAction,
    SetCompletionAction,
    canonical_action_projection,
    reminder_matches_action,
)


class DueStartVerificationTests(unittest.TestCase):
    """Exercise the GUI-observed provider normalization and its boundaries."""

    def cases(self):
        before = {
            "title": "GUI-created task",
            "notes": None,
            "url": None,
            "location": None,
            "priority": 0,
            "completed": False,
            "due": None,
            "start": None,
            "alarms": [],
            "recurrence_rules": [],
            "list_id": "synthetic-list",
        }
        patch = {
            "notes": "A note with a newline\nand Korean 한국어",
            "url": "https://example.com/reminder",
            "priority": 5,
            "due": {"kind": "all_day", "date": "2026-09-07"},
        }
        action = PatchAction(patch)
        after = {**before, **patch}
        derived_start = {
            "kind": "timed",
            "date_time": None,
            "local_date_time": "2026-09-07T00:00:00",
            "time_zone": None,
            "floating": True,
        }
        yield "provider retains null", before, action, after, True
        normalized = {**after, "start": derived_start}
        yield "provider adds exact floating midnight", before, action, normalized, True
        for name, changed in (
            ("wrong day", {"local_date_time": "2026-09-08T00:00:00"}),
            ("wrong time", {"local_date_time": "2026-09-07T09:00:00"}),
            ("fixed zone", {"time_zone": "Asia/Seoul"}),
            ("absolute instant", {"date_time": "2026-09-06T15:00:00Z"}),
            ("not floating", {"floating": False}),
            ("unknown component", {"unrecognized": "value"}),
        ):
            yield (
                name,
                before,
                action,
                {**after, "start": {**derived_start, **changed}},
                False,
            )
        for field, value in (
            ("notes", "lost note"),
            ("url", None),
            ("priority", 0),
            ("due", {"kind": "all_day", "date": "2026-09-08"}),
            ("alarms", [{"kind": "relative", "offset_seconds": 0}]),
            ("recurrence_rules", [{"frequency": "daily", "interval": 1}]),
            ("list_id", "other-list"),
            ("completed", True),
            ("location", "unexpected place"),
            ("title", "unexpected title"),
        ):
            yield (
                f"other stable field drifts: {field}",
                before,
                action,
                {**normalized, field: value},
                False,
            )
        for missing in ("start", "due"):
            incomplete_before = {key: value for key, value in before.items() if key != missing}
            yield f"missing before {missing}", incomplete_before, action, normalized, missing == "start"
        existing_start = {**derived_start, "local_date_time": "2026-09-05T08:30:00"}
        started_before = {**before, "start": existing_start}
        yield "existing authored start preserved", started_before, action, {
            **after, "start": existing_start
        }, True
        yield "existing authored start replaced", started_before, action, normalized, False
        scheduled_before = {**before, "due": {"kind": "all_day", "date": "2026-09-06"}}
        yield "existing due does not justify start drift", scheduled_before, action, normalized, False
        previously_normalized = {**before, "due": patch["due"], "start": derived_start}
        next_day = PatchAction({"due": {"kind": "all_day", "date": "2026-09-08"}})
        rescheduled = {**previously_normalized, **next_day.patch}
        yield "later due change preserves original derived start", previously_normalized, next_day, rescheduled, True
        yield "later due change cannot move original derived start", previously_normalized, next_day, {
            **rescheduled, "start": {**derived_start, "local_date_time": "2026-09-08T00:00:00"}
        }, False
        for name, other_action in (
            ("title change", PatchAction({"title": "Updated"})),
            ("completion", SetCompletionAction(True)),
            ("move", MoveToListAction("other-list")),
        ):
            expected = canonical_action_projection(scheduled_before, other_action)
            yield name, scheduled_before, other_action, {
                **expected, "start": {**derived_start, "local_date_time": "2026-09-06T00:00:00"}
            }, False
        timed_action = PatchAction({"due": {
            "kind": "timed",
            "date_time": "2026-09-07T08:30:00.000+09:00",
            "time_zone": "Asia/Seoul",
        }})
        yield "timed due does not use all-day normalization", before, timed_action, {
            **before, **timed_action.patch, "start": derived_start
        }, False
        timed_due = timed_action.patch["due"]
        timed_after = {**before, "due": timed_due, "start": timed_due}
        yield "first zoned due synthesizes the same zoned start", before, timed_action, timed_after, True
        yield "first zoned due may retain absent start", before, timed_action, {
            **timed_after, "start": None
        }, True
        for name, start_patch in (
            ("wrong instant", {"date_time": "2026-09-07T08:31:00.000+09:00"}),
            ("lost time zone", {"time_zone": None}),
            ("different named time zone", {"time_zone": "Asia/Tokyo"}),
            ("floating marker", {"floating": True}),
        ):
            yield f"first zoned start rejects {name}", before, timed_action, {
                **timed_after, "start": {**timed_due, **start_patch}
            }, False
        floating_due = {
            "kind": "timed", "floating": True,
            "local_date_time": "2026-09-07T08:30:00", "date_time": None, "time_zone": None,
        }
        yield "zoned due never downgrades both fields to floating", before, timed_action, {
            **timed_after, "due": floating_due, "start": floating_due
        }, False
        yield "zoned due never downgrades only start to floating", before, timed_action, {
            **timed_after, "start": floating_due
        }, False
        yield "first zoned due cannot lose existing authored start", started_before, timed_action, timed_after, False
        yield "first zoned due preserves existing authored start", started_before, timed_action, {
            **timed_after, "start": existing_start
        }, True
        yield "timed normalization cannot hide alarm drift", before, timed_action, {
            **timed_after, "alarms": [{"kind": "relative", "offset_seconds": 0}]
        }, False
        floating_action = PatchAction({"due": {
            "kind": "timed", "floating": True,
            "local_date_time": "2026-09-10T12:00:00",
        }})
        floating_canonical = {
            **floating_action.patch["due"], "date_time": None, "time_zone": None,
        }
        floating_after = {**before, "due": floating_canonical, "start": floating_canonical}
        yield "first floating due synthesizes exact local start", before, floating_action, floating_after, True
        yield "first floating due may retain absent start", before, floating_action, {
            **floating_after, "start": None
        }, True
        for name, start_patch in (
            ("other wall time", {"local_date_time": "2026-09-10T12:01:00"}),
            ("named time zone", {"time_zone": "Asia/Seoul"}),
            ("absolute timestamp", {"date_time": "2026-09-10T12:00:00+09:00"}),
            ("non-floating marker", {"floating": False}),
            ("unknown component", {"unrecognized": "value"}),
        ):
            yield f"first floating start rejects {name}", before, floating_action, {
                **floating_after, "start": {**floating_canonical, **start_patch}
            }, False
        yield "first floating due cannot replace existing start", started_before, floating_action, floating_after, False
        yield "first floating due preserves existing start", started_before, floating_action, {
            **floating_after, "start": existing_start
        }, True
        yield "floating normalization cannot hide omitted notes drift", before, floating_action, {
            **floating_after, "notes": "unexpected change"
        }, False
        yield "floating normalization cannot hide alarm drift", before, floating_action, {
            **floating_after, "alarms": [{"kind": "relative", "offset_seconds": 0}]
        }, False
        yield "floating normalization cannot hide due zone change", before, floating_action, {
            **floating_after, "due": {**floating_canonical, "time_zone": "Asia/Seoul"}
        }, False

    def test_python_timed_start_uses_existing_timestamp_equivalence(self):
        before = {"due": None, "start": None, "notes": "Stable"}
        action = PatchAction({"due": {
            "kind": "timed", "date_time": "2026-09-08T14:00:00+09:00", "time_zone": "Asia/Seoul",
        }})
        canonical_due = {
            **action.patch["due"], "date_time": "2026-09-08T14:00:00.000+09:00",
        }
        self.assertTrue(reminder_matches_action({
            **before, "due": canonical_due, "start": canonical_due,
        }, before, action))

    def test_python_verifies_only_observed_due_start_normalization(self):
        for name, before, action, after, expected in self.cases():
            with self.subTest(case=name):
                before_copy = copy.deepcopy(before)
                after_copy = copy.deepcopy(after)
                projection = canonical_action_projection(before, action)
                self.assertEqual(reminder_matches_action(after, before, action), expected)
                self.assertEqual(before, before_copy)
                self.assertEqual(after, after_copy)
                self.assertEqual(canonical_action_projection(before, action), projection)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS EventKit")
    def test_native_receipt_verification_matches_python_boundaries(self):
        # Compile the real static verifier without entering its normal main or
        # constructing an EventStore. No access to the user's reminders occurs.
        native_source = SCRIPTS / "reminders_eventkit.m"
        harness = f'''
#define main apple_reminders_unused_main
#include {json.dumps(str(native_source))}
#undef main
int main(void) {{
    @autoreleasepool {{
        NSData *input = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
        NSArray *cases = [NSJSONSerialization JSONObjectWithData:input options:0 error:nil];
        NSMutableArray *results = [NSMutableArray array];
        for (NSDictionary *entry in cases) {{
            NSMutableDictionary *projection = [entry[@"projection"] mutableCopy];
            NSDictionary *due = projection[@"due"];
            // Native action projections canonicalize the closed floating
            // input to the full read shape before mutation verification.
            if ([due isKindOfClass:[NSDictionary class]] &&
                due.count == 3 && [due[@"floating"] isEqual:@YES]) {{
                projection[@"due"] = CanonicalDueVerificationValue(due);
            }}
            [results addObject:@(MutationProjectionMatches(entry[@"before"], projection, entry[@"after"]))];
        }}
        NSData *output = [NSJSONSerialization dataWithJSONObject:results options:0 error:nil];
        [[NSFileHandle fileHandleWithStandardOutput] writeData:output];
        return 0;
    }}
}}
'''
        cases = list(self.cases())
        inputs = [
            {"before": before, "projection": canonical_action_projection(before, action), "after": after}
            for _, before, action, after, _ in cases
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "due-start.m"
            binary = Path(directory) / "due-start"
            source.write_text(harness, encoding="utf-8")
            compiled = subprocess.run(
                ["clang", "-x", "objective-c", "-fobjc-arc", "-framework", "Foundation",
                 "-framework", "EventKit", "-framework", "CoreLocation", str(source), "-o", str(binary)],
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run(
                [str(binary)], input=json.dumps(inputs), capture_output=True,
                text=True, timeout=10, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(len(actual), len(cases))
        for (name, _, _, _, expected), observed in zip(cases, actual):
            with self.subTest(case=name):
                self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
