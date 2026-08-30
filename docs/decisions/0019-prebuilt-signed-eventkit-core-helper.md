# 0019 — Ship a signed, notarized EventKit Core helper

**Status:** Accepted — rollout pending

## Context

Core reminder operations currently compile `reminders_eventkit.m` on each
user's Mac, cache the result, and apply an ad-hoc signature. That makes Xcode
Command Line Tools an ordinary-user dependency, increases first-run latency,
and gives the helper a less stable code-signing identity across rebuilds. The
Python bridge around it already owns the important safety rules: bounded input,
typed normalization, mutation uncertainty, Receipt validation, and exact
read-back. Replacing that bridge would increase risk without improving the
installation experience.

Codex marketplace installation copies the plugin subtree from a reviewed Git
tag. A helper attached only to a GitHub Release would therefore not reach an
ordinary marketplace installation. A bare executable can be notarized, but its
ticket cannot be stapled for offline Gatekeeper verification.

## Options considered

1. Keep compiling on every user's Mac. This preserves a source-only artifact,
   but keeps the largest setup dependency and unstable ad-hoc identity.
2. Download a helper at first run. This removes the compiler but introduces a
   network bootstrap, update service, extraction path, and an additional trust
   decision outside the reviewed plugin tag.
3. Commit a raw signed executable. This is smaller than an app bundle, but a
   notarization ticket cannot be stapled directly to it.
4. Commit a signed, notarized, stapled universal app bundle and execute its
   inner binary directly through the existing JSON stdin/stdout protocol.

## Decision

Choose option 4 for the public EventKit Core helper, using a two-pull-request
rollout so the current source-built runtime keeps working until the notarized
app is ready.

The future release bundle is
`native/AppleRemindersEventKitHelper.app`, with bundle identifier
`io.github.oscar-v4.apple-reminders.eventkit-bridge`. It contains exactly one
universal `arm64` and `x86_64` executable, targets macOS 14, uses Developer ID
Application signing, Hardened Runtime, a secure timestamp, Apple notarization,
and a stapled ticket. App Sandbox remains off and the helper has no
entitlements. Reminders access is requested through EventKit and the reviewed
purpose string; no undocumented entitlement is invented.

The first pull request adds only the build, verification, signing preparation,
package policy, and this decision record. It keeps the existing
`eventkit_bridge_info.plist`, source compiler, ad-hoc helper identity, and
runtime resolver unchanged. The signed app is then produced from that merged,
versioned source commit.

The second pull request atomically commits the reviewed app and provenance
manifest and switches ordinary Core resolution to that app. At that point the
Python bridge remains the public safety boundary. Normal execution will resolve
only the bundled app, reject symlinks and signature or identity drift, and
launch the inner executable directly so synchronous stdin/stdout semantics do
not change. A missing or invalid bundle will fail before mutation as
`native_helper_unavailable`; it will not trigger an automatic compiler
fallback. Contributor source compilation will remain an explicit development
operation using the legacy plist and identity.

The bundle identifier intentionally migrates once from the existing
`com.codex` ad-hoc identity to the repository owner's namespace. Both identities
remain separate during the rollout and the signed identity must remain stable
after release. Users may see one new Reminders permission prompt at the
migration boundary.

Release preparation is separate from release publication:

1. A secret-free job tests the exact default-branch commit, builds and executes
   an ad-hoc universal candidate, records source and build-input hashes, checks
   the ZIP central directory, and uploads an immutable unsigned artifact.
2. A protected job checks that artifact's digest, validates it before
   extraction, and uses only macOS system tools plus short inline checks while
   signing credentials exist. It never checks out repository code or executes
   the candidate. It signs, notarizes, staples, emits the final manifest and
   checksums, destroys credentials, and only then uploads the signed artifact.
3. A second secret-free job downloads the exact signed bytes, verifies their
   digests, runs the full repository verifier including schema and capability
   probes, validates Developer ID, ticket, Gatekeeper, source hashes, and
   modes, then attests and uploads those exact reviewed files.
4. The expanded bundle and provenance manifest enter the repository through a
   normal reviewed pull request.
5. A later secrets-free tag workflow must verify the committed artifact's
   attestation, require its source commit to be on the default branch, and
   verify the deterministic plugin ZIP before publication.

The package allowlist admits only the exact app members and provenance manifest.
The executable alone keeps mode `0755`; every other packaged file is normalized
to `0644`. Unexpected files, special files, symlinks, modes, architectures,
entitlements, identifiers, source hashes, build inputs, or provenance fail
release checks.

## Consequences

Ordinary Core use no longer requires Xcode Command Line Tools once the second
pull request and its signed bundle are released. Until then, the existing
source-built behavior remains unchanged. Python 3.11+ remains a runtime
dependency for the MCP and safety layers. Advanced sections, image attachments,
and Recently Deleted recovery still use private helper paths that compile
locally; those capabilities temporarily retain the Command Line Tools
dependency and must be documented separately rather than implying the whole
plugin is compiler-free.

The signed bytes and notarization ticket are intentionally not reproducible
from source alone because secure timestamps and Apple tickets vary. The tagged
repository instead contains one fixed, reviewed byte sequence bound to source,
tooling, a source commit, checksums, and a GitHub artifact attestation. The
enclosing plugin ZIP remains deterministic.

Fresh-profile testing must cover permission denial and approval, direct helper
launch versus Codex child launch, and an update from one signed helper version
to the next at a changed plugin path. A fixed Application Support installation
path is not introduced without evidence that the in-plugin path loses TCC
continuity. The private helpers may later adopt the same distribution pattern,
but that is a separate decision because their framework and OS compatibility
risks differ from public EventKit Core.
