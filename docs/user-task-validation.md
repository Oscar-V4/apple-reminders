# Realistic user-task validation

Use this procedure when a changed installation path, public tool/skill,
mutation behavior, or backend becomes usable end to end, and before release
signoff. Choose the scenario yourself from the changed behavior and likely
user intent. Run a representative journey when a milestone is ready rather
than waiting for the user to discover the next integration failure.

## Choose and execute

1. Write a natural user request and observable completion criteria **before**
   operating the plugin. Combine the changed capability with existing state,
   such as an image plus a due date and alarm. Include input acquisition when
   the request requires it: pre-existing icon files alone do not test browser
   screenshot capture. The examples below are starting points, not an exhaustive
   suite; invent or vary a task that exposes the changed boundary.
2. Run relevant deterministic checks, then select one complete journey and,
   when the change affects failure/recovery, one relevant interruption case.
   Reuse the existing skills' `evals/evals.json`, source tests, packaged MCP
   tools, and `scripts/live_smoke.py` where they cover the need. A missing
   scenario warrants a bounded extension, not a second test framework.
3. Exercise the exact candidate or installed package through the normal
   launcher and public MCP, using the same skill/tool route an agent would use
   for that request. Discover the actual tool schema and diagnose a requested
   Native capability according to its current skill. Record the package
   identity, mode, environment, and permission state. A source-function unit
   test or a successful helper compilation is separate evidence.
4. Use existing maintainer/user authorization and disposable synthetic fixtures
   for autonomous acceptance. Resolve an exact writable account/list, record
   ownership before creating anything, and retain exact fixture identifiers
   privately for cleanup. Keep both the deadline and every calculated alarm
   trigger safely beyond the expected test and cleanup window; a future
   deadline alone can still produce an imminent or past relative alert.
   User-owned reminders are not reusable test fixtures.
   CI remains synthetic and performs no live store
   reads, framework loads, permission prompts, or writes.
5. Check the user's outcome through fresh public reads after each meaningful
   mutation, including the requested change and unrelated fields that must
   survive. For images, verify every intended attachment and its identity;
   count alone is insufficient. Observe native rendering when claiming visible
   presentation, and keep Mac rendering, CloudKit metadata, and direct iPhone
   observation separate. Inspect whether the agent's final answer accurately
   reports completed and incomplete parts of the original request.
6. A stale reference, pending/partial result, or unavailable capability is an
   outcome to investigate. Follow the receipt's bounded read-only recovery and
   preserve original error/uncertainty. Do not replay an unresolved mutation,
   invent a new idempotency key to bypass it, widen admission, or silently
   replace requested images with note links. When a defect is established,
   add a focused regression, fix it, then exercise a freshly prepared affected
   journey. Resume a prior fixture only when its outcome and authority are
   resolved. Stop repeating a passing journey until another relevant change
   or unresolved concern justifies it.
7. Verify cleanup of only the exact synthetic items/list owned by this run.
   Record retained or unknown cleanup honestly; do not clear Recently Deleted.
   Close the scenario with evidence for every completion criterion or an
   explicit unmet criterion. Unsupported environments remain a recorded
   limitation, not evidence that the requested feature worked.

Documentation-only changes do not trigger live mutations. For larger work,
an independent agent can act as the scenario operator or review its evidence;
choose reasoning effort for the task's difficulty and keep the task bounded.

## Starting scenarios

| Natural request | Observable completion criteria |
| --- | --- |
| “I'm considering this research program. Capture the poster and explanation in about six screenshots, put them in one reminder with space for ideas, set its deadline, and remind me two days before.” | One intended reminder in the exact selected list; all six intended images acquired and individually verified; note space and source URL; typed all-day deadline; relative alarm `-172800`; no unintended URL card; truthful final answer. The fixture uses a synthetic notice and a future deadline. |
| “The deadline moved. Change it, but keep my notes, photos, and two-day warning.” | New due date and due-relative alarm agree; every image, note, priority, and unrelated field survives; no new reminder or duplicate alarm. |
| “Move this application task to the other project's list; keep everything I've collected.” | Exact account/list identity despite duplicate display names; due, complete alarm state, notes, URL, images, and unrelated fields preserved; source item is moved once without unrequested copies/deletions. |
| “The attachment attempt was interrupted. Check what happened and finish only what's missing.” | Fresh reads resolve the exact operation where possible; no blind retry, duplicate image, or fabricated success; uncertainty and any remaining action are explicit; verified cleanup or a retained/unknown fixture record. Use deterministic injection for unsafe or unavailable failure conditions and label it accordingly. |

A passed component does not prove the composed task. In the September 2026
work, the original six-image user task and the later single-image candidate
acceptance were different runs. A separate alarm-bearing fixture encountered
a version race and was cleaned up. Combining those records does not establish
a new-release six-image-plus-alarm acceptance result.

## Evidence and follow-up

Keep one short local record per scenario with these fields:

- Natural request; change under test; completion criteria.
- Exact source commit/package digest or installed release; launcher mode;
  actual hardware/macOS/Reminders and permission/developer-tool conditions.
- Steps attempted; observed results and evidence; each unmet criterion.
- Overall result: passed, blocked, incomplete, or failed. Separately record
  cleanup as verified, retained, unknown, or not created.
- Linked regression/fix and any subsequent fresh run; remaining limitations.

Keep private fixture IDs, paths, raw receipts, and screenshots outside the
repository. Commit only redacted evidence in `docs/release-evidence/` and link
it from the PR/signoff. The [external tester workflow](launch/external-tester-workflow.md)
provides a closed receipt format for its supported scenarios; format validation
and synthetic examples are not proof of execution. If the exact scenario has
no matching format, use the record above instead of relabeling it to fit.

Tie each claim to the tested package. A candidate result is not automatically
a later release or clean-user result; a source-only fix is not installed until
its verified distribution is installed. This procedure changes development
practice without granting new runtime capability or access.
