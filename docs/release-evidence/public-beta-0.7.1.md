# v0.7.1 public beta publication evidence

The [v0.7.1 release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.7.1)
is a published **immutable public beta**, with `isPrerelease:true` and
`isImmutable:true`. This is not a general-availability release.

| Identity | Verified value |
| --- | --- |
| Tag commit | `664a83638ebf021151c585308c83358208a15bb6` |
| Tag object | `8c9290d868d20b6b68c41167f4a5e7eb8ebc03f7` |
| Release workflow | [34065545655](https://github.com/Oscar-V4/apple-reminders/actions/runs/34065545655), all four jobs successful |
| PR #61 CI | [34064845781](https://github.com/Oscar-V4/apple-reminders/actions/runs/34064845781), all four jobs successful at `2c0026c3cf9e8c6b8e910cf096c337f74f6afec8` |

The immutable release contains exactly these assets:

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `apple-reminders-0.7.1.zip` | 55005987 | `531a79b980707facb6d0a2def183510f5a6fc536b7cc0e1e9d85dd72ae0dcebc` |
| `SHA256SUMS` | 92 | `f8f6ffdfbb40e1a005e3f455d5ac6d3aaf3b0e53406dcb13df2e677ee2610563` |

The release workflow's final verifier and a separate local canonical
`python3 scripts/verify_release_assets.py v0.7.1` run passed. Recorded results
include exact asset verification, release attestation, source audit,
byte-identical deterministic rebuild, and EventKit, Native, and bundled Python
provenance verification.

The [signoff history](patch-candidate-0.7.1.md) retains the earlier signing runs,
local test totals, and candidate ZIP hash. That checkpoint hash identifies its
own tested bytes; the hashes above identify the immutable published release.
Subsequent repository documentation and receipt-format additions do not change
these release assets. The [v0.7.0 record](public-beta-0.7.0.md) and its launch/tester
packet remain historical evidence for that earlier release.

Fresh-user no-CLT acceptance has **not** been established. The separately added
`fresh_native_image_no_clt` scenario validates a closed self-reported format;
its synthetic example is not a real tester receipt. Existing-permission image
testing on the maintainer Mac remains distinct from clean-user, Intel/minimum
macOS, section/tag, and direct iPhone evidence. No installation/reload outcome
for the user's current Codex session or social announcement is recorded here.
