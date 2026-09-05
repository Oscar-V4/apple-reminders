# Apple Reminders 0.6.0 public beta launch kit

This is the maintained launch packet for the `v0.6.0` release candidate. This
documentation change does not create a tag or GitHub Release, change repository settings, record a demo, or post to a
social channel. Candidate commands and announcement copy become usable only
after every publication gate below passes for the exact tag.

## Candidate source of truth

- Version: the plugin manifest declares `0.6.0`; publication requires a matching
  signed EventKit helper and the intended immutable ref `v0.6.0`.
- Distribution: independent, open-source community plugin from this GitHub
  repo marketplace.
- Stable Core: documented EventKit operations targeting macOS 14+, with a
  bundled Python runtime, no separate Python installation, explicit Reminders
  permission, and no Xcode or Xcode Command Line Tools. Startup lists 9 Core and
  diagnostic tools; 6 additional experimental tools require `--experimental`.
  Default URL writes store EventKit URL metadata only.
- Experimental Internals: private, version-sensitive Reminders interfaces with
  two dependency tiers. Compiler-free private paths cover tag mutation,
  URL-only attachment mutation, and bounded read-only native inspection.
  CLT-required private paths cover section mutation, image-attachment mutation,
  and exact Recently Deleted inspection or recovery.
- Privacy boundary: the plugin-owned stdio MCP and adapters run on the Mac and
  have no plugin-owned remote backend, but tool results return to Codex under
  the user's Codex product, account, and privacy terms.

Every private mutation or exact recovery requires an exact four-part allowlist
match for macOS version/build and Reminders version/build plus the exact
command-schema fingerprint. Missing identity reports `runtime_unverified`; a
complete but unlisted identity reports `unsupported_build`. Neither condition
disables Stable Core.

For CLT-required private paths, runtime admission uses fixed
`/usr/bin/xcode-select -p`, rejects developer environment overrides, ignores
`PATH` clang entries and the `/usr/bin/clang` installer shim, and accepts only
the fixed compiler path under the selected developer directory. Compiler
presence is a dependency check, never private-interface compatibility evidence.

## Signed Core and verification evidence

Publication requires Developer ID signatures, notarization, stapled tickets
and verified source provenance for the EventKit helper and both Python
capsules. Read the exact candidate manifests rather than copying source hashes
into this document:

- [EventKit helper manifest](../../plugins/apple-reminders/native/eventkit-helper-build.json)
- [Apple silicon Python manifest](../../plugins/apple-reminders/runtime/python-runtime-build-arm64.json)
- [Intel Python manifest](../../plugins/apple-reminders/runtime/python-runtime-build-x86_64.json)

The canonical verifier checks these manifests against the exact release's
source and workflow history. A link is not a substitute for that verification.

Core existing-item changes use one canonical alarm projection: the requested
delta is combined with stable user-authored state, alarm arrays compare as
order-insensitive multisets with duplicate counts, and provider-owned derived
fields are excluded. Title changes, completion/reopen, and list moves cannot
return `verified` or a new Reference when absolute, location, writable-relative,
or read-only alarms are lost or transformed. This is local exact read-back
evidence, not proof of sync to every device or shared-list participant.

## Publication and candidate install gate

The command that will install the candidate pins one exact ref:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.6.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Do not distribute these commands as a working release until all of the
following are true:

1. `v0.6.0` resolves to the intended merge commit and the manifest, changelog,
   signed-helper manifest, ZIP name, and checksum inventory agree.
2. The tag workflow emits one two-subject SLSA statement for the deterministic
   ZIP and `SHA256SUMS`, verifies it before the only contents-write step, and
   publishes an immutable two-asset GitHub Release.
3. From an exact clean tag checkout, the canonical verifier succeeds:

   ```bash
   python3 scripts/verify_release_assets.py v0.6.0
   ```

   It must re-download both assets, verify the immutable release and SLSA
   attestations, bind the tag and canonical GitHub main history, audit source,
   rebuild the ZIP twice byte-for-byte, and verify the EventKit helper and both Python runtime provenance records.
4. The final read-only macOS verification job is green and the release URL
   resolves without authentication.

`SHA256SUMS` detects corruption but is not a signature by itself. The canonical
no-new-secret authenticity path is the immutable release plus the separately
verified SLSA subjects and `release.yml` workflow identity. Refreshing an
existing repo-marketplace entry keeps its configured ref; moving versions
requires removing and re-adding the plugin and marketplace at the reviewed tag,
then starting a new Codex task.

## Deferred external evidence

Repository tests and signing evidence do not replace these environment checks:

| Check | Current evidence boundary | Tracking |
| --- | --- | --- |
| Fresh Stable Core install and TCC allow/deny with no external Python, Xcode or Command Line Tools | Needs a new macOS user, disposable VM, or separate Mac | [#28](https://github.com/Oscar-V4/apple-reminders/issues/28) |
| Real Intel Reminders operations | Native runtime probes are required in CI; they do not establish full Reminders CRUD behavior | [#30](https://github.com/Oscar-V4/apple-reminders/issues/30) |
| Exact minimum macOS 14 execution | Deployment-target metadata is not runtime evidence | [#30](https://github.com/Oscar-V4/apple-reminders/issues/30) |
| Upgrade permission identity on an external subject | Signing provenance exists; external TCC continuity remains unproven | [#28](https://github.com/Oscar-V4/apple-reminders/issues/28) |
| CLT-only Experimental admission or fail-closed behavior | Fixed compiler selection and synthetic tests are not external execution evidence | [#41](https://github.com/Oscar-V4/apple-reminders/issues/41) |
| Privacy-reviewed external receipts | No broader compatibility claim until receipts exist | [#30](https://github.com/Oscar-V4/apple-reminders/issues/30) |
| Synthetic 60–90 second demo and GitHub social-preview setting | Asset exists; recording and repository setting remain separate manual actions | [#29](https://github.com/Oscar-V4/apple-reminders/issues/29) |

Use the [external tester workflow](external-tester-workflow.md) only after the
publication gate passes. It accepts a closed redacted receipt and never asks
for Reminder content, identifiers, logs, screenshots, hashes, or local paths.

## 60–90 second synthetic demo

Record only after the publication gate passes. Use a clean macOS test subject,
or hide every non-demo Reminder and all account UI. Name the disposable list
`Codex Beta Demo` plus the recording's UTC hour. If permission was already
granted, state that on the title card instead of staging a prompt.

| Time | Screen and action | Narration boundary |
| --- | --- | --- |
| 0–8 s | Title card: “Apple Reminders for Codex · community plugin · public beta.” | “Stable Core uses the local MCP and signed EventKit helper; tool results return to Codex.” |
| 8–18 s | Show the two pinned candidate commands and the successful canonical verifier result, then start a new Codex task. | “The install is pinned to one immutable tag whose release assets were verified again.” |
| 18–30 s | Ask for one bounded list read. If the clean subject shows the native Reminders prompt, grant access once. | “Stable Core needs Reminders permission and does not need Xcode.” |
| 30–48 s | Create synthetic Reminders including one due item with a relative alarm, using only `https://example.com/demo` if a URL is shown. | “A successful write requires the canonical alarm projection and a fresh exact read-back.” |
| 48–61 s | Change one synthetic title and complete/reopen another item, then read the list again. | “Alarm and stable user state must survive unrelated changes before a Receipt can say verified.” |
| 61–74 s | Delete every synthetic Reminder and verify that the active list is empty. | “Cleanup targets only synthetic items and stops on any uncertain Receipt.” |
| 74–88 s | In Reminders, delete the exact empty disposable list, then show the release and issue links. | “External testing reports only a validated redacted receipt.” |

Do not reset TCC, empty Recently Deleted, show account names or notifications,
paste terminal paths, or record another app. Review every frame before upload.
Deleted demo items may remain in the system's Recently Deleted area; that does
not establish recovery or active-list failure.

## Social preview provenance

The maintained preview is
[`docs/launch/assets/apple-reminders-social-preview.png`](assets/apple-reminders-social-preview.png),
a version-neutral 1200×630 image introduced by source commit
`9e86c384ce9463df3e97b2cb88441c7341fde033`. It was derived from the
project-owned cover with OpenAI image editing and visually checked at full and
thumbnail size. Its SHA-256 is:

```text
91a3f60e194eab13c1dc04a89492d3b13fdc83d24e002a5dc0a94e73c48ed140
```

Visible copy:

```text
Apple Reminders for Codex
Independent · Local MCP · Open source
Notes & screenshots
```

The image's presence in the repository does not assert that the GitHub social
preview setting was changed; that separate manual action remains in issue #29.

## Pre-post gate

- Re-run `python3 scripts/check_public_claims.py` on the exact commit whose copy
  will be used.
- Require the tag, immutable Release, canonical verifier, and final read-only
  workflow to pass before using the drafts below.
- Keep Stable Core, compiler-free private, and CLT-required private claims
  separate; an exact allowlist pass does not make an Experimental path Stable.
- Link the privacy boundary and state that tool results return to Codex.
- Omit a demo link until the synthetic recording passes frame review.
- Omit compatibility, adoption, performance, or platform-affiliation claims
  that lack public evidence.
- Do not change repository settings, create a tag or Release, or post to a
  social channel as part of this documentation PR.

## Korean SNS draft

Use only after the publication gate passes.

> Apple Reminders for Codex `v0.6.0` 공개 베타를 배포했습니다. 독립
> 오픈소스 커뮤니티 프로젝트입니다.
>
> macOS 14 이상을 대상으로 하며 Python 실행환경을 함께 제공합니다.
> Reminders 권한을 허용하면 기본 작업에 Python이나 Xcode를 따로 설치할
> 필요가 없습니다. 기본 도구 9개를 제공하고 실험 기능은 직접 선택해 켭니다.
> Experimental Internals는 exact build/schema allowlist를 통과해야 하며,
> 일부 경로만 선택된 Command Line Tools compiler를 사용합니다.
>
> 플러그인 소유 원격 백엔드는 없지만 tool results return to Codex라는
> 경계가 있습니다. 개인정보 안내:
> https://github.com/Oscar-V4/apple-reminders/blob/v0.6.0/PRIVACY.md
>
> 설치:
> `codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.6.0`
> `codex plugin add apple-reminders@oscar-v4-reminders`
>
> 릴리스 검증 방법:
> https://github.com/Oscar-V4/apple-reminders/blob/v0.6.0/docs/release-verification.md
> synthetic data만 사용하는 외부 테스트:
> https://github.com/Oscar-V4/apple-reminders/issues/30
>
> Apple 또는 OpenAI와 제휴 관계가 없는 프로젝트입니다.

## English SNS draft

Use only after the publication gate passes.

> Apple Reminders for Codex `v0.6.0` is available as a public beta from an
> independent, open-source community project.
>
> Stable Core targets macOS 14+ and uses bundled Python with a signed EventKit
> helper. It needs Reminders permission, with no separate Python installation or
> Xcode. Nine tools are available by default; experimental features are opt-in.
> Experimental Internals require an exact build/schema allowlist match, and
> only some paths use the selected Command Line Tools compiler.
>
> There is no plugin-owned remote backend, but tool results return to Codex.
> Privacy boundary:
> https://github.com/Oscar-V4/apple-reminders/blob/v0.6.0/PRIVACY.md
>
> Install:
> `codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.6.0`
> `codex plugin add apple-reminders@oscar-v4-reminders`
>
> Verify the release:
> https://github.com/Oscar-V4/apple-reminders/blob/v0.6.0/docs/release-verification.md
> Privacy-safe synthetic Mac testers are welcome:
> https://github.com/Oscar-V4/apple-reminders/issues/30
>
> This project has no Apple or OpenAI affiliation.

These are drafts only. Posting them is a separate representational action.
