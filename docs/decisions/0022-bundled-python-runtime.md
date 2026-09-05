# 0022 — Bundle the Python runtime for ordinary installation

**Status:** Implementation in progress; distribution requires signed candidates
and release verification

## Context

The public Core backend already ships a signed EventKit helper. Its surrounding
MCP transport and safety layers still need Python 3.11 or newer. Asking a person
to install Python, Homebrew, or developer tools before creating a reminder is
an avoidable installation dependency. Finding a system Python can also select
an Apple developer-tool installer shim.

Rewriting the tested transport, opaque References, idempotency, and mutation
verification in another language would put unrelated behavior at risk. A
first-run Python download would introduce a second installation and trust path
after the marketplace has installed the reviewed plugin.

## Decision

Keep the Python implementation and distribute a pinned CPython runtime with
the plugin. The runtime consists of two architecture-specific capsules, for
Apple silicon and Intel Macs. Each capsule holds a Developer ID signed,
notarized, stapled `AppleRemindersPythonRuntime.app`; its stable bundle
identifier is `io.github.oscar-v4.apple-reminders.python-runtime`. An app wrapper
makes a stapled offline notarization ticket possible. The EventKit helper and
its Reminders permission identity remain separate.

The reviewed runtime lock records the upstream release, exact download names,
URLs, byte lengths and SHA-256 digests. The unsigned builder preserves upstream
Python licensing and component metadata, removes archive indirections by
materializing safe in-tree links, and records the complete resulting inventory.
The runtime app targets macOS 14 and retains one CPU architecture per capsule.
No user computer compiles, signs, or downloads Python during ordinary startup.

Capsules and their provenance belong under `runtime/`, outside `native/`.
Updating the EventKit helper currently replaces its complete native directory.
Separate directories also make the two component boundaries explicit.

The runtime loader validates the selected capsule and its app before execution.
Its installation/cache policy must preserve complete-byte verification, reject
unexpected links and file types, and avoid exposing a partly extracted app as
ready. Missing or invalid runtime content fails with an actionable message;
it must not silently search for another interpreter or launch developer tools.

## Trusted preparation

`prepare-signed-runtime-source.yml` uses the same four permission domains as the
supported EventKit source-preparation workflow:

1. A job without secrets checks out one exact source commit. The owner must
   dispatch the workflow from the current protected default-branch head. The
   job proves the selected remote source branch still names that commit, the
   trusted workflow commit is its ancestor, and the workflow definition is
   identical in both commits. It builds both unsigned architecture candidates,
   runs data-free checks, and uploads their inventory manifests and checksums.
2. A protected `release-signing` job has no checkout and never executes a
   candidate, its interpreter, or source scripts. Trusted inline validation
   checks the complete artifact inventory and digest chain before extraction.
   It bounds archive sizes, counts, paths, modes and file types; independently
   identifies Mach-O files; checks architecture and bundle identity; signs
   nested native code before the enclosing app; and notarizes and staples.
   Credentials are deleted before signed artifacts and output digests leave
   the job.
3. A verification job has neither secrets nor OIDC/attestation permissions.
   It checks the exact signed bytes and source provenance, exercises the actual
   runtimes only in this unprivileged job, and verifies Gatekeeper and nested
   signatures. A matrix uses `macos-15` for arm64 and `macos-15-intel` for Intel,
   checks the actual host architecture, and requires both native executions to
   pass. These labels are documented in the [GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
   Both matrix jobs rehash all protected subjects after source-code tests.
4. A final job has no checkout and receives only the exact verified artifact.
   It compares the complete subject inventory and digests against both signing
   and verification outputs before producing one GitHub SLSA attestation.

Workflow infrastructure must be merged and reviewed on main before it can
prepare candidates from another source branch. The manifest binds source and
trusted workflow commits separately. The prepared capsules enter the
repository through the same reviewed source-and-artifact change. The tag
release remains secrets-free and verifies runtime source ancestry and the
exact signer-workflow attestation before packaging.

The existing EventKit signing workflow is unchanged. Its manifest binds the
plugin version, so a plugin version bump still requires a freshly prepared
EventKit helper even when its native source is unchanged. Runtime provenance is
separate and must not be fabricated by editing signed manifests locally.

## Verification and claims

CI uses only synthetic requests, imports and metadata. It must not access a
real Reminders store, trigger the Reminders permission prompt, launch the
Reminders app or perform mutations. Complete assembled-plugin validation runs
after the final-version EventKit helper and both Python capsules are present.

Before describing a release as requiring no separate Python installation,
verify the installed capsule with a restricted environment lacking external
Python and developer tools. Test paths with spaces, subprocess interpreter
reuse, standard-library native imports, signature failure, damaged archives,
and clean restart after interrupted extraction. Test both CPU architectures;
Rosetta execution is evidence for translated execution, not an Intel Mac test.
A clean Mac installation, quarantine/Gatekeeper behavior and permission flows
remain explicit release evidence and cannot be inferred from unit tests alone.

Signed native bytes and Apple tickets are not reproducible from source alone.
One reviewed, attested byte sequence is committed; packaging that fixed sequence
into the plugin ZIP remains deterministic. Runtime updates should follow
upstream supported CPython releases and security fixes, with new provenance,
signatures, notarization and regression evidence for changed runtime bytes.
