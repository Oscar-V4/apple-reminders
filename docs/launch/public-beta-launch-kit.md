# Apple Reminders 0.4 release and community launch kit

The tagged GitHub pre-release is public. This kit records its verified identity
and separates completed release gates from the remaining demo, Showcase,
Developer Forum, and SNS actions. Replace the remaining angle-bracket
placeholders, delete unsupported claims, and use copy from this file only after
the corresponding gate is green.

The product should be described as a **community-built, open-source, local-first
Apple Reminders plugin for Codex**. It is distributed through a GitHub repo
marketplace. It is not an Apple product, an OpenAI product, an OpenAI-endorsed
plugin, or a current listing in the universal ChatGPT and Codex Plugins
Directory. The reasons and official routes are documented in the [community
publishing research](../research/openai-community-publishing-2026-08.md).
Here, “local-first” describes the plugin-owned MCP and adapters, not the Codex
service boundary: tool results return to the Codex host process and may be
processed under the user's Codex product and account terms. Link the [privacy
boundary](../../PRIVACY.md) whenever the phrase appears in standalone copy.

## Release placeholders

Fill these once from the final tagged release and reuse the same values
everywhere:

| Placeholder | Final value |
| --- | --- |
| `<TAG>` | `v0.4.0` |
| `<RELEASE_URL>` | `https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.4.0` |
| `<COMMIT_SHA>` | `f8eb9e25e6eec486cbf99650bc74ec57f1d5de65` |
| `<ZIP_URL>` | `https://github.com/Oscar-V4/apple-reminders/releases/download/v0.4.0/apple-reminders-0.4.0.zip` |
| `<ZIP_SHA256>` | `a0f53ae4053053abe75541e68ae35b01f904ff4a013b99b14e686b95fa1355da` |
| `<CI_URL>` | `https://github.com/Oscar-V4/apple-reminders/actions/runs/33257175249` |
| `<CLEAN_INSTALL_EVIDENCE_URL>` | `https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-evidence/v0.4.0.md#exact-tag-clean-install` |
| `<LIVE_SMOKE_EVIDENCE_URL>` | `https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-evidence/v0.4.0.md#live-apple-reminders-smoke` |
| `<BENCHMARK_EVIDENCE_URL>` | `https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-evidence/v0.4.0.md#data-free-release-benchmark` |
| `<DEMO_URL>` | Public 60–90 second demo |
| `<COVER_IMAGE_URL>` | `https://raw.githubusercontent.com/Oscar-V4/apple-reminders/main/docs/launch/assets/apple-reminders-v0.4.0-cover.png` |
| `<ISSUES_URL>` | `https://github.com/Oscar-V4/apple-reminders/issues/new/choose` |
| `<MODEL_BUILD_NOTES>` | Codex desktop; a separately orchestrated WebGPT Agent Pro audit found a bug in public PR #19. Describe it as cross-agent AI review, not independent human review. Do not add a model-specific claim without a linkable record. |

## Launch gates

### Before any announcement

- [x] Merge the reviewed candidate; create `<TAG>` from the intended
  `<COMMIT_SHA>`; keep the tag, manifest, changelog, ZIP filename, and release
  notes on the same version.
- [x] Build the allowlisted archive from that tag twice and confirm identical
  bytes and `<ZIP_SHA256>`. Publish both `<ZIP_URL>` and the checksum.
- [x] Confirm `<CI_URL>` is green for the tagged commit and that the documented
  Python/macOS matrix is accurate. Do not copy a test count from an older run.
- [x] On an isolated Codex home that does not use the development checkout, run
  the README's exact tagged marketplace add/install commands and save a
  redacted report at `<CLEAN_INSTALL_EVIDENCE_URL>`.
- [x] Verify MCP initialization, the expected 15-tool Interface, targeted
  diagnosis, and a first Core read from the installed artifact. Record the
  actual Python selected by the launcher.
- [x] Run the opt-in live smoke against one uniquely named synthetic list,
  prove exact cleanup, and publish only content-free status/latency evidence at
  `<LIVE_SMOKE_EVIDENCE_URL>`. Never publish personal reminder data.
- [x] Review [README](../../README.md), [privacy](../../PRIVACY.md),
  [security](../../SECURITY.md), [support](../../SUPPORT.md),
  [terms](../../TERMS.md), and [license](../../LICENSE) links from the tag.
- [x] Confirm the first README screen clearly states macOS, Python, Xcode
  command-line tool, permission, local-MCP, private-interface, install, and
  support boundaries.
- [ ] Record and review `<DEMO_URL>` using only synthetic content. Crop or hide
  the Reminders sidebar, notifications, account names, menu-bar data, file
  paths, and any other personal information.
- [x] Use one consistent public author name across GitHub, plugin metadata,
  social profiles, and the Showcase draft. Confirm rights to the logo, cover,
  video, music, and screenshots.
- [ ] Test `<ISSUES_URL>` while logged out and prepare issue labels or a short
  report template for install, permission, Core, Native Extension, and privacy
  reports.
- [x] Read the final announcement aloud once. Remove “official,” “approved,”
  “certified,” “partner,” “production-ready,” or any unverified adoption or
  performance claim.

The release-evidence gate is green, but the full announcement gate still needs
the synthetic demo and final channel checks. As of 2026-08-29, the official
community “Submit a project” link resolves to the Showcase gallery and exposes
no public submission form or control; the former standalone form returns 403.
Do not guess or bypass a submission endpoint. Keep the packet ready and recheck
the official route.

### After release and announcement

- [ ] Re-run the exact tagged install from the public repository, not a branch,
  and compare the installed version and artifact checksum with the release.
- [ ] Triage first-user reports promptly; turn confirmed failures into minimal
  reproductions, regression tests, fixes, and changelog entries.
- [ ] Ask testers for macOS version, Codex/plugin version, Python source, and
  redacted Receipt/error category—not reminder titles, notes, attachments, or
  account identifiers.
- [ ] Keep the project post and the local-device MCP product-feedback post
  separate. The first recruits users; the second discusses a platform gap.
- [ ] Recheck the official OpenAI community and Showcase pages. Submit only if
  they expose a public submission action; say “submitted” only after it
  succeeds and “featured” only if an OpenAI-hosted page is actually published.
- [ ] Track durable evidence: external install reports, issues, fixes,
  discussions, forks, and contributions. Do not manufacture testimonials,
  stars, download counts, or endorsements.
- [ ] Recheck requirements and install steps on every release. Preserve a
  working pinned install for the previous tag when feasible.
- [ ] After credible adoption, evaluate Codex for Open Source, a future Codex
  Ambassadors cohort, or MCP Registry packaging as separate opportunities—not
  as badges implied by this launch.

## SNS drafts

### Korean

> Apple Reminders용 커뮤니티 제작 오픈소스 Codex 플러그인 `<TAG>` 공개
> 베타를 배포했습니다.
>
> 로컬 stdio MCP와 macOS 어댑터가 Reminders를 읽고 수정합니다. 일상
> 작업은 EventKit을 우선하고, 섹션·태그·첨부처럼 버전에 민감한 기능은
> 별도 경계와 읽기 검증을 둡니다. 설치와 첫 사용은 영상에서 1분 안에
> 확인할 수 있습니다: `<DEMO_URL>`
> 플러그인 소유 원격 백엔드는 없지만 도구 결과는 Codex로 반환됩니다.
> 개인정보 경계: `https://github.com/Oscar-V4/apple-reminders/blob/<TAG>/PRIVACY.md`
>
> 설치: `codex plugin marketplace add Oscar-V4/apple-reminders --ref <TAG>`
> 후 `codex plugin add apple-reminders@oscar-v4-reminders`
>
> macOS 14+, Python 3.11+, Xcode command-line tools, Reminders 권한이
> 필요합니다. 릴리스·체크섬·검증 자료: `<RELEASE_URL>` / `<CI_URL>` /
> `<CLEAN_INSTALL_EVIDENCE_URL>`
>
> 실제 Mac에서 테스트해주실 분을 찾습니다. 문제 제보:
> `<ISSUES_URL>`. OpenAI나 Apple의 공식·보증 플러그인은 아닙니다.
> 선택 태그: `@OpenAIDevs`

If the public history supports it, add one final sentence: “Codex를 사용해
구현하고 별도 AI 에이전트 감사와 실제 Reminders 스모크 테스트로
점검했습니다.”
Otherwise omit it.

### English

> I released `<TAG>` of Apple Reminders for Codex, a community-built,
> open-source public beta.
>
> Its local stdio MCP and macOS adapters read and update Reminders. Routine
> work prefers EventKit; version-sensitive sections, tags, and attachments are
> isolated behind explicit capability and read-back checks. See the 60–90
> second install and workflow demo: `<DEMO_URL>`
> There is no plugin-owned remote backend, but tool results return to Codex.
> Privacy boundary: `https://github.com/Oscar-V4/apple-reminders/blob/<TAG>/PRIVACY.md`
>
> Install: `codex plugin marketplace add Oscar-V4/apple-reminders --ref <TAG>`,
> then `codex plugin add apple-reminders@oscar-v4-reminders`.
>
> Requires macOS 14+, Python 3.11+, Xcode command-line tools, and Reminders
> permission. Release, checksum, and verification evidence:
> `<RELEASE_URL>` / `<CI_URL>` / `<CLEAN_INSTALL_EVIDENCE_URL>`
>
> I am looking for Mac testers: `<ISSUES_URL>`. This is not an official or
> endorsed OpenAI or Apple plugin. Optional tag: `@OpenAIDevs`

If publicly supported, add: “Built with Codex and checked through automated,
cross-agent AI, and live Apple Reminders tests.” Do not imply independent human
review, or name a model or test count unless its linked evidence matches the
tag.

## 60–90 second disposable demo

Use a clean macOS test user or hide all non-demo Reminders content. Name the
list `Codex Demo <UTC timestamp>` so it cannot be confused with an existing
list. The public MCP does not expose list deletion, so the last cleanup step is
performed visibly in the Reminders app.

| Time | Screen and action | Suggested narration |
| --- | --- | --- |
| 0–8 s | Title card: project name, `<TAG>`, “community-built / local MCP / public beta.” | “The plugin-owned MCP and adapters run on my Mac; their tool results return to Codex.” |
| 8–18 s | Show the two exact pinned marketplace install commands, then start a new Codex task. | “The release installs from a pinned GitHub repo marketplace tag.” |
| 18–28 s | Ask: `Codex Demo <UTC timestamp> 목록을 만들어줘.` If macOS asks for Reminders permission, approve only the explicit plugin request. | “First use asks for Reminders access only when the requested operation needs it.” |
| 28–43 s | Ask: `그 목록에 '오트밀크 사기', '<DEMO_DATE>에 테스트 메모 보내기', '예제 문서 읽기'를 추가하고 마지막 항목 URL은 https://example.com/demo 로 설정해줘.` | “Core capture uses EventKit. When the URL Receipt says verified, its final exact read passed; otherwise I show the pending or partial result.” |
| 43–54 s | Ask: `Codex Demo <UTC timestamp> 목록의 미완료 항목만 보여줘.` | “Reads are bounded and exact list identity is preserved.” |
| 54–65 s | Ask: `'오트밀크 사기'를 완료하고 다시 읽어서 확인해줘.` | “Changes return a read-back Receipt rather than treating process exit as success.” |
| 65–78 s | Ask: `Codex Demo <UTC timestamp>의 리마인더를 모두 정확히 삭제하고 결과를 확인해줘.` Show the empty list. | “The demo removes only its synthetic reminders and verifies the result.” |
| 78–90 s | In Reminders, manually delete the exact empty `Codex Demo <UTC timestamp>` list; show the release and issue links. | “The public beta is open source; install evidence and issue reporting are linked with the release.” |

Do not reset TCC, empty **Recently Deleted**, or delete a similarly named list
for the recording. macOS may retain deleted synthetic content in Recently
Deleted; that recoverable system behavior should not be described as a failed
active-list cleanup. If permission was already granted, say so or use a title
card—do not stage a fake prompt.

## OpenAI Developer Showcase draft

**Route status (2026-08-29):** the official [OpenAI community
page](https://developers.openai.com/community) advertises “Submit a project,”
but its link currently resolves to the [Showcase
gallery](https://developers.openai.com/showcase), where no public submission
form or control is exposed. The former standalone form returns 403. Keep this
draft ready, do not guess another endpoint, and recheck the official route.

**Submission gate:** use this only if an official public submission action
returns, and only after `<TAG>`, `<RELEASE_URL>`, the public setup instructions,
and `<COVER_IMAGE_URL>` resolve without authentication. Submission would not
mean acceptance or endorsement.

- **Project title:** Apple Reminders for Codex
- **Tagline:** Safer Apple Reminders workflows from Codex on macOS.
- **Displayed author:** `Sungsoo Kim (Oscar-V4)`
- **Public project URL:** `https://github.com/Oscar-V4/apple-reminders`
- **Tagged release:** `<RELEASE_URL>`
- **Demo:** `<DEMO_URL>`
- **Cover image:** `<COVER_IMAGE_URL>`
- **Was Codex used?:** Yes.
- **Models and coding agents:** `<MODEL_BUILD_NOTES>`
- **OpenAI API:** `N/A — the released plugin does not itself require an OpenAI API key` (re-verify against the tag and the current form).
- **Capabilities / use cases:** Local tool calling for reminder capture,
  bounded briefing, exact updates, completion/reopen, organization, and
  image/URL attachment workflows.
- **Technical stack:** Codex plugin manifest and skills; local Python stdio MCP;
  Objective-C/macOS EventKit adapters; narrower version-sensitive native
  adapters; deterministic source ZIP; GitHub Actions.
- **Project description:**

  > Apple Reminders for Codex is a community-built, open-source macOS plugin
  > that lets a person brief, capture, organize, and safely update the
  > Reminders data available to their local Mac. Routine operations prefer
  > EventKit. Sections, tags, and native attachments are isolated behind
  > explicit capability and read-back checks. The public beta deliberately
  > exposes a bounded 15-tool Interface and withholds broad maintenance and
  > backup operations. It documents permission, local-data, private-interface,
  > concurrency, and verification limits instead of claiming that a process
  > exit proves an Apple-device sync. The plugin-owned MCP has no remote
  > backend, but its tool results return to Codex and are subject to the user's
  > Codex product and account terms.

- **How it was built:**

  > The project grew from repeated real Reminders use and regression fixes. I
  > used Codex to inspect, implement, test, and review the repository while
  > keeping behavior behind explicit module and MCP contracts. Before the
  > public beta, I reduced the public surface, preserved idempotency and
  > stale-write protections, added deterministic packaging and clean-install
  > checks, and exercised a disposable synthetic workflow against Apple
  > Reminders. Evidence: `<CI_URL>`, `<CLEAN_INSTALL_EVIDENCE_URL>`, and
  > `<LIVE_SMOKE_EVIDENCE_URL>`.

- **Reproducible setup:**

  ```bash
  codex plugin marketplace add Oscar-V4/apple-reminders --ref <TAG>
  codex plugin add apple-reminders@oscar-v4-reminders
  ```

  Start a new Codex task, ask for a bounded Reminders read, and approve the
  explicit macOS Reminders permission request if needed. Requirements and
  troubleshooting are in the [tagged README](../../README.md).

Before submitting, re-open the official [community
page](https://developers.openai.com/community) and [Showcase
gallery](https://developers.openai.com/showcase), confirm that they expose a
real public submission action, and check its current fields and asset rules.
Confirm that all claims are supported by the [release
regression contract](../regression-contract.md), [public tool
schema](../../plugins/apple-reminders/schemas/mcp-tools.json), release evidence,
and rights-owned assets. Save the submitted text and date privately. Announce
“submitted” only after the form succeeds; link a Showcase page only if OpenAI
publishes one.

## Developer Forum posts

### Project post: released beta and tester request

Suggested title: **Apple Reminders for Codex `<TAG>` — local-MCP public beta,
looking for Mac testers**

Outline:

1. One sentence about the problem: natural Codex workflows need safe access to
   the user's native Reminders without turning the integration into a hosted
   data service.
2. State the distribution truth: community-built, open source, local stdio MCP,
   tool results return to Codex, tagged GitHub repo marketplace release; not a
   universal-directory listing.
3. Show three supported goals: bounded briefing, exact capture/update, and
   sections/tags/attachments with explicit capability limits.
4. Paste the two pinned install commands and the minimum requirements.
5. Link `<DEMO_URL>`, `<RELEASE_URL>`, `<CI_URL>`, and the [privacy
   boundary](../../PRIVACY.md).
6. Briefly explain guarded writes: opaque references, idempotency, stale-write
   rejection, and exact read-back Receipts. Link the [release
   contract](../regression-contract.md), not a marketing superlative.
7. Ask for a small, concrete test matrix: Intel/Apple silicon if available,
   macOS versions within the documented range, Homebrew/python.org Python, and
   Core versus Native Extension outcomes.
8. Ask reporters to use `<ISSUES_URL>` and redact reminder/account content.
9. End with: “This is an independent community project and is not endorsed by
   OpenAI or Apple.”

### Product-feedback post: device-bound local MCP distribution

Post separately after the project post has a stable release link. Suggested
title: **Feedback: a reviewed distribution path for local, device-bound MCP
servers**

Outline:

1. Describe the general platform problem, not a request to waive review for
   this repository: some native integrations must execute on the user's device
   because macOS permissions and data are local to that process.
2. Cite the current public-HTTPS production MCP requirement and the separate
   developer-tunnel role from [OpenAI's documented publishing
   constraints](../research/openai-community-publishing-2026-08.md#current-local-stdio-limitation).
3. Cite Apple's per-process EventKit permission model from the same research.
   Explain that a hosted relay cannot simply inherit the user's local Reminders
   permission.
4. Explain the cost of forcing a hosted architecture: local agent, identity,
   per-user routing, privacy, security, and operations become new product
   surfaces.
5. Offer reviewable safeguards for discussion: signed/notarized executables,
   immutable package hashes and provenance, explicit permission disclosure,
   closed tool schemas, destructive/read-only annotations, bounded local file
   access, update/revocation rules, and installed-version visibility.
6. Ask two concrete questions: “Is a reviewed local-executable distribution
   path planned?” and “What package, signing, consent, and update evidence would
   OpenAI need to evaluate one?”
7. Link this project only as a worked example with public evidence. Do not call
   it rejected, submitted, approved, or representative of OpenAI's roadmap.

## Portfolio and resume copy

Use these only after the tagged release. Keep the evidence links in a portfolio
version; shorten them for a resume while retaining the repository URL.

### Korean

- Apple Reminders의 실제 사용 실패 사례를 15개 도구의 제한된 Codex
  플러그인 인터페이스로 재설계하고, EventKit 우선 Core와 버전 민감 Native
  Extension을 분리해 오픈소스 공개 베타로 배포함. [인터페이스
  명세](../regression-contract.md#public-tool-surface) · `<RELEASE_URL>`
- 불투명 revision 참조, 생성 idempotency, stale-write 차단, 최종 exact
  read-back Receipt를 구현해 동시성과 부분 성공을 단순 성공으로 오인하지
  않도록 함. [회귀 계약](../regression-contract.md) · [README 동작
  설명](../../README.md#references-and-mutation-results)
- 테스트·개발 파일을 제외하는 allowlist 기반 결정적 ZIP과 고정 태그
  repo-marketplace 설치 경로를 만들고, 격리된 설치 결과와 체크섬을 공개함.
  `<CLEAN_INSTALL_EVIDENCE_URL>` · `<ZIP_URL>` · `<ZIP_SHA256>`
- 합성 목록만 사용하는 실제 Apple Reminders 스모크 테스트로 생성 재시도,
  제한 조회, stale revision, URL·이미지·섹션, 완료·재개·삭제와 exact cleanup을
  검증함. `<LIVE_SMOKE_EVIDENCE_URL>`
- 광범위 백업 복원·대량 유지보수·UI handoff를 0.4 공개 인터페이스에서
  의도적으로 제외하고, exact Recently Deleted 복구와의 경계 및
  권한·로컬 데이터·private interface·검증 한계를 문서화함.
  [설계 판단](../regression-contract.md#deliberately-withheld-behavior) ·
  [privacy](../../PRIVACY.md)

### English

- Designed and released a bounded 15-tool Codex plugin for Apple Reminders,
  separating EventKit-first Core operations from version-sensitive native
  extensions. [Interface specification](../regression-contract.md#public-tool-surface)
  · `<RELEASE_URL>`
- Implemented opaque revision references, create idempotency, stale-write
  rejection, and exact read-back Receipts so concurrency and partial outcomes
  are not reported as generic success. [Regression
  contract](../regression-contract.md) · [behavior
  documentation](../../README.md#references-and-mutation-results)
- Built an allowlisted deterministic source artifact and pinned GitHub
  repo-marketplace install, then published checksum and isolated-install
  evidence. `<ZIP_URL>` · `<ZIP_SHA256>` · `<CLEAN_INSTALL_EVIDENCE_URL>`
- Exercised representative Core and Native Extension flows against Apple
  Reminders using a disposable synthetic list with exact cleanup evidence.
  `<LIVE_SMOKE_EVIDENCE_URL>`
- Reduced release risk by withholding broad maintenance, backup/restore, and
  unverifiable UI-handoff operations while documenting permission, local-data,
  private-interface, and verification boundaries. [Release
  decision](../regression-contract.md#deliberately-withheld-behavior) ·
  [privacy policy](../../PRIVACY.md)

Do not add download, user, star, performance, acceptance, or feature-placement
metrics until a durable source supports each number. A passing submission is
not a Showcase feature, and a GitHub marketplace release is not an OpenAI
Plugins Directory listing.

## Asset and evidence checklist

### Public assets

- [ ] Repository social preview and `<COVER_IMAGE_URL>` use the released name,
  legible community/local-MCP wording, the caption links the Codex result
  boundary, and all artwork is rights-owned.
- [ ] `<DEMO_URL>` is captioned, readable without audio, 60–90 seconds, and
  contains no personal reminders, accounts, notifications, paths, or secrets.
- [ ] One static screenshot shows the exact pinned install commands; one shows
  a synthetic read-back Receipt; neither implies OpenAI or Apple endorsement.
- [ ] README badges link to the tagged/default-branch CI source rather than a
  manually entered pass image.
- [ ] Author photo/name, GitHub bio, repository description, pinned-project
  placement, and portfolio URL are consistent.

### Release evidence

- [x] `<RELEASE_URL>` points to `<TAG>` at `<COMMIT_SHA>` and contains release
  notes, requirements, upgrade notes, known limits, `<ZIP_URL>`, and
  `<ZIP_SHA256>`.
- [x] `<CI_URL>` shows the supported runtime matrix at the tagged commit.
- [x] `<CLEAN_INSTALL_EVIDENCE_URL>` records disposable Codex/Home directories,
  exact tag/SHA, installed file boundary, initialization, tool discovery,
  selected Python, and first non-mutating Core result.
- [x] `<LIVE_SMOKE_EVIDENCE_URL>` records only operation name, status, latency,
  and exact cleanup outcome for a reserved synthetic list.
- [x] `<BENCHMARK_EVIDENCE_URL>` states machine, OS, Python, samples, warmups,
  and subprocess wall-time definition; it does not present a cross-machine
  speed score.
- [x] The [public schema](../../plugins/apple-reminders/schemas/mcp-tools.json),
  [architecture](../architecture.md), [release
  contract](../regression-contract.md), [changelog](../../CHANGELOG.md), and
  user-facing policies are reachable from the release.
- [x] Any “built/audited with Codex” claim links to a public build history or
  cross-agent AI audit artifact and does not imply independent human review.
  Any external-user claim links to a public issue, contribution, or
  permissioned testimonial.

### Final human check

- [ ] A person unfamiliar with the repository can answer in two minutes: what
  it does, how to install it, what permission it needs, what runs locally, that
  tool results return to Codex, what uses private interfaces, how writes are
  verified, how to uninstall it, and where to report a problem.
- [ ] A reviewer can reproduce the release without the development checkout.
- [ ] Every public sentence remains true if the project is never featured by
  OpenAI. Recognition should add evidence to the story, not make the story
  true.
