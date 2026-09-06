# v0.7.0 public beta publication evidence

The [v0.7.0 release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.7.0)
is published as an **immutable public beta**: `isPrerelease:true` and
`isImmutable:true`. This is not a general-availability release.

## Exact published identity

| Field | Verified value |
| --- | --- |
| Tag | `v0.7.0` |
| Tag object | `869fe87a59b01edbe1d39b746df807f20733ed75` |
| Peeled tag commit | `2cefe47db3e226599f358b2e4c96fb56c12a6578` |
| Release workflow | [34058848985](https://github.com/Oscar-V4/apple-reminders/actions/runs/34058848985), all four jobs successful |
| PR #56 CI | [34058248242](https://github.com/Oscar-V4/apple-reminders/actions/runs/34058248242), all four jobs successful at `2ee135d` |

The immutable release contains exactly these two assets:

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `apple-reminders-0.7.0.zip` | 54999635 | `e5ea5f8598b3ff49d176ecaa6841e6d0ac75e597b0e075368c53affc0e60768a` |
| `SHA256SUMS` | 92 | `e3576150279fc1e88d78f565d7b9939ec6a41cde26938ab8b28c7e30e5f9a582` |

## Verification results

The release's final read-only job ran the canonical redownload verifier.
A separate local `python3 scripts/verify_release_assets.py v0.7.0` run also
passed. Both checked the published assets rather than relying only on their
names or on checksum text. Recorded results:

- Release attestation and exact two-asset inventory verified.
- Source audit verified; deterministic rebuild byte-identical.
- Signed EventKit helper manifest/provenance verified.
- Signed Native helper manifest, expanded app, and provenance verified.
- Bundled Python runtime provenance verified.

The final local suite completed 1,035 tests with one skipped test and an overall
successful result. Installed-package smoke passed in all three modes: default
15 tools, core-only nine tools, and legacy URL mode 15 tools. These are package
validation results, not a claim that a user's existing Codex task has reloaded
the new plugin.

The [signoff history](release-candidate-signoff.md) preserves earlier signing
runs and candidate smoke hashes. Those earlier ZIP hashes identify earlier
checkpoints; the asset hashes above identify the immutable published release.
Subsequent documentation updates do not change these published bytes. The
immutable tag's bundled documents retain their pre-publication checkpoint
wording; this post-publication record updates the release status without
rewriting the tag or archive.

## Remaining evidence boundaries

Clean-user TCC/CRUD and upgrade acceptance, real Intel and minimum-macOS
operation coverage, section/tag compatibility, and direct iPhone observation
remain separate from public beta publication. The admitted-host
[synthetic Native image flow](v0.7.0-native-image-acceptance.md) is bounded
operation evidence, not universal Native capability support. No private
Reminder title, identifier, attachment content, or user account data is included
here. No social announcement or demo publication is asserted by this record.
