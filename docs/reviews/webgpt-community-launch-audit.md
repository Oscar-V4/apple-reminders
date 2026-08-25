# Apple Reminders 0.3 — Community Launch and Career Contribution Audit

**Audit target:** [`79b1afb4384e8837e1a9f0a88b666960ec820c91`](https://github.com/Oscar-V4/apple-reminders/commit/79b1afb4384e8837e1a9f0a88b666960ec820c91)  
**Base branch:** `codex/public-beta-0.3`  
**Audit date:** 2026-08-26  
**Requested deliverable:** report only; no runtime, test, README, marketplace, legal, release, issue, or existing pull-request changes  
**Publication boundary:** this report drafts material but does not merge, tag, release, submit a form, post to a forum or social network, or contact anyone

## 1. Evidence method and confidence labels

This audit treats the candidate commit as immutable. Repository facts come from the exact candidate, draft PR #3, issue #2, and the exact-head GitHub Actions run. Current OpenAI program and publication facts come only from official OpenAI documentation. Comparator facts come from the maintainers' repositories and tagged releases.[^C-COMMIT][^C-PR3][^C-ISSUE2][^C-CI]

The requested isolated WebGPT Agent backend failed before it could create a worktree and also failed on a read-only repository request. The audit therefore used the authorized GitHub connector against the immutable SHA. No Apple Reminders process, clean macOS account, second Mac, local test suite, or live MCP workflow was available in this execution environment. Statements about the candidate's existing live run are therefore **confirmed first-party evidence**, not independently reproduced evidence.[^C-PR3][^C-ISSUE2]

Labels used below:

- **Confirmed:** directly supported by an official OpenAI source or first-party repository/release evidence.
- **Audit judgment:** an evaluation made from confirmed evidence.
- **Inference:** a conclusion that follows from confirmed facts but is not itself stated by a source.
- **Recommendation:** proposed work or positioning; not a claim about current implementation.

## 2. Executive decision

### 2.1 Product and beta decision

**Audit judgment — conditional GO for a repo-marketplace public beta, but NO-GO for publication today until the remaining release gates are closed.**

The candidate is a technically credible local-first product: it exposes a static 13-tool surface, prefers EventKit for Core work, isolates version-sensitive Native Extension work, uses exact identity and opaque references, preserves idempotency and stale-write rejection, and returns scoped mutation receipts with exact read-back semantics.[^C-README][^C-ARCH][^C-SPEC] The exact candidate has a successful macOS 14 CI matrix on Python 3.11 and 3.12, and PR #3/issue #2 record deterministic packaging and a redacted disposable live workflow on the owner's current machine.[^C-CI][^C-PR3][^C-ISSUE2]

The unresolved blockers are release operations, not a reward for reducing tool count: a genuinely clean marketplace install and first permission flow, durable evidence tied to that clean environment, and a final merged/tagged/checksummed release identity remain open in the project's own release record.[^C-ISSUE2] The current 8 Core / 4 Native Extension / 1 Diagnostics split is defensible because each boundary maps to a recognizable user outcome; tool count by itself is not a quality metric.[^C-SPEC][^C-ARCH]

### 2.2 Official OpenAI Plugins Directory

**Explicit answer: NO for the current candidate; NOT YET for this product architecture.**

OpenAI currently directs plugins with only local `stdio` MCP servers to expose a stable public HTTP endpoint or wait until local MCP servers are supported. Public MCP submission requires a stable, publicly reachable HTTPS endpoint using streamable HTTP; a local endpoint, temporary tunnel, or Secure MCP Tunnel alone does not satisfy public submission requirements.[^O-LOCAL][^O-MCP] After review and publication, eligible plugins appear in the universal Plugins Directory shared by ChatGPT and Codex.[^O-SUBMIT]

The candidate's `.mcp.json` launches `python3 ./mcp/server.py` locally, and its product value depends on access to the current Mac user's native Apple Reminders store.[^C-MCP][^C-PRIVACY] A hosted endpoint cannot truthfully perform that operation unless there is a real, separately designed user-local agent or pairing channel that can reach the local store. OpenAI's public endpoint requirements also say the production endpoint must reach the required services and data stores.[^O-MCP]

**Audit judgment:** do not submit the current full product to the universal directory, do not claim directory eligibility, and do not present a public proxy as equivalent to direct local Reminders access. A skills-only submission is technically a supported OpenAI submission type, but it would not provide the native Reminders tools that define this product; marketing it as the same integration would be misleading.[^O-SUBMIT][^O-ARCH]

**Recommendation:** keep “official Plugins Directory” off the beta critical path. Re-evaluate only when OpenAI supports local MCP submissions, or after a separately reviewed paired-local architecture exists with explicit authentication, local availability, privacy, failure, and support boundaries.

### 2.3 Feasible official and public-recognition paths

**Repo/local marketplace: YES, technically feasible after release blockers.** OpenAI distinguishes local and repo marketplaces from the universal public directory and describes them as authoring, testing, and team-distribution sources.[^O-PACKAGE] The exact candidate has a repository marketplace entry pointing only to `./plugins/apple-reminders`, a plugin manifest, and a bundled local MCP declaration.[^C-MARKET][^C-MANIFEST][^C-MCP]

**OpenAI developer Showcase/community project: YES, feasible as a separate project submission; acceptance is not guaranteed.** The official Community page invites builders to submit a project, demo, or workflow to the OpenAI developer Showcase.[^O-COMMUNITY] Current Showcase entries include GitHub-backed community projects, native macOS projects, and local-first projects, so a truthful local Apple Reminders engineering case study is not disqualified merely because it is not a hosted SaaS plugin.[^O-SHOWCASE][^O-CODEX101][^O-SWIFTY][^O-WAVEFORM] This route recognizes the project and build story; it does not make the plugin eligible for the universal Plugins Directory.

**GitHub and SNS career proof: YES, independently feasible.** A release, reproducible evidence bundle, demo, technical write-up, and public repository can establish career evidence without claiming OpenAI approval. The OpenAI Community page links the Developer Forum, Discord, Reddit, and X as community spaces, but posting there is not an official product endorsement.[^O-COMMUNITY]

**Codex Ambassadors: not an immediate route.** Applications are currently paused; OpenAI says a future cohort will open later. The program is for community organizers, open-source maintainers, student leaders, and power users, with recurring community contributions rather than one-off product submission.[^O-AMBASSADORS]

**Codex for Open Source: possible support route, not a listing or endorsement.** OpenAI invites core maintainers or maintainers of widely used public projects and says projects outside the usual criteria may still apply and explain their ecosystem importance. Selection and benefits are discretionary.[^O-OSS][^O-OSS-TERMS] **Inference:** Apple Reminders 0.3 may be early for a “widely used” case, but a later application could be credible after public users, issue activity, releases, and maintenance evidence exist.

**Developer Forum: useful discussion route, not official recognition.** OpenAI positions the forum for questions, implementation comparisons, and community support.[^O-COMMUNITY] Post only after the release evidence is stable enough that discussion does not become unpaid first-install debugging.

## 3. Recognition route matrix

| Route | Current answer | What it actually means | Minimum credible evidence | Recommendation |
|---|---|---|---|---|
| Universal ChatGPT/Codex Plugins Directory | **No / not yet** | One reviewed public listing shared by ChatGPT and Codex; current MCP path requires public HTTPS/streamable HTTP.[^O-LOCAL][^O-SUBMIT] | A truthful supported architecture, verified publisher identity, public MCP endpoint, auth, accurate tool metadata/annotations, five positive and three negative tests, availability, and release notes.[^O-REVIEW][^O-SUBMIT] | Do not submit this local-only build or create a fake remote equivalent. |
| Git-backed repo marketplace | **Yes after beta blockers** | Public repository distribution outside the universal directory.[^O-PACKAGE] | Exact tag, marketplace entry, installable subtree, clean install, support docs, checksum, tag-install verification. | Primary 0.3 distribution path. |
| Personal/local marketplace | **Yes for development** | Authoring/testing distribution; not official directory publication.[^O-PACKAGE] | Exact candidate and local install evidence. | Continue using for development and release rehearsal. |
| Workspace distribution | **Potentially, by workspace policy** | Private/team availability governed by workspace controls; not universal public publication.[^O-PACKAGE][^O-APPS] | Admin-approved package/app configuration and role controls. | Secondary, only if an actual workspace adopter exists. |
| OpenAI developer Showcase | **Feasible separate submission** | Editorial/community project recognition, not plugin approval.[^O-COMMUNITY][^O-SHOWCASE] | Public release, repository, concise story, screenshots/video, build notes, evidence links, exact OpenAI product usage. | Submit after the P0 release evidence and demo are complete. |
| Developer Forum | **Feasible after release** | Peer discussion and implementation learning.[^O-COMMUNITY] | Reproducible setup, known limitations, focused technical question or case study. | Use for technical discussion, not a launch approval claim. |
| Codex Ambassadors | **Unavailable now** | Ongoing community leadership program; applications paused.[^O-AMBASSADORS] | Repeated workshops/resources/community contribution. | Revisit when applications reopen and there is a community track record. |
| Codex for Open Source | **Possible later** | Maintainer support benefits, not directory placement or endorsement.[^O-OSS][^O-OSS-TERMS] | Public adoption, active maintenance, ecosystem relevance, maintainer role. | Revisit after several releases and external usage evidence. |
| GitHub/SNS/portfolio | **Yes** | Independent career proof controlled by the owner. | Immutable release, CI, demo, evidence ledger, honest limitations, issue/support history. | Publish only after P0; never call it an “official OpenAI plugin.” |

## 4. Candidate product audit

### 4.1 Human product proposition

**Confirmed.** The candidate is a macOS-only local Codex plugin that reads and changes the current user's Apple Reminders through a local `stdio` MCP server. Core operations prefer EventKit; sections, tags, and native image/URL attachment behavior use narrower version-sensitive interfaces.[^C-README][^C-ARCH]

**Audit judgment.** The strongest product story is not “13 tools.” It is: ordinary Reminder work through a bounded Core path, with advanced native features isolated behind capability checks and explicit verification limits. The product becomes credible when a user can complete one useful task in the first five minutes and understand exactly which process receives Reminders permission.

### 4.2 Reliability and trust strengths

The following are confirmed candidate promises or first-party evidence:

- Exact list/reminder identity rather than display-name identity, including duplicate-name handling.[^C-README][^C-SPEC]
- Bounded reads, filter-bound cursors, closed inputs, and preserved omitted fields.[^C-SPEC][^C-ARCH]
- Create idempotency; one-use opaque references; stale, expired, consumed, and concurrent-write rejection.[^C-README][^C-ARCH]
- Mutation receipts that distinguish verified, pending, partial, unchanged, and failed outcomes, with `verified` scoped to named evidence rather than broad sync claims.[^C-README][^C-PRIVACY]
- Core work remains available when a Native Extension capability is unavailable.[^C-README][^C-ARCH]
- Diagnostics are failure-triggered and content-free rather than an onboarding preflight.[^C-README][^C-ARCH]
- The exact candidate's CI run completed successfully for Python 3.11 and 3.12 on macOS 14 and included manifest/schema checks, synthetic tests, Objective-C syntax checks, and deterministic packaging checks.[^C-CI]
- PR #3 and issue #2 record two local 353-test runs, a deterministic 48-file candidate ZIP, SHA-256 `1242027aeb7b6fd6d23f37aeec6c6c67749d960ff5419ee1f1360602c0b45323`, exact-package 13-tool discovery, and a redacted disposable live workflow on the owner's current machine.[^C-PR3][^C-ISSUE2]

### 4.3 Trust and support risks

**Confirmed.** The documented requirements are macOS 14 or newer, Python 3.11 or newer, and Xcode command-line tools while native helpers ship as source. The MCP manifest resolves ambient `python3`.[^C-README][^C-MCP]

**Confirmed.** The plugin has no plugin-owned remote endpoint, but results return to the Codex host; “local MCP” does not mean Reminder content necessarily remains outside the Codex service boundary. Changes may sync through Apple services and affect shared-list participants.[^C-PRIVACY]

**Confirmed.** Advanced features use private or version-sensitive macOS interfaces, and the project does not promise compatibility with every future Reminders schema, direct iPhone inspection, automatic restore, ChatGPT web, Codex cloud, Windows, or Linux.[^C-PRIVACY][^C-SUPPORT]

**Audit judgment.** The candidate's semantic reliability is stronger than its installation reliability. The highest beta risk is not an optimistic write result; it is environment variance before the first successful tool call: Codex's resolved Python, Xcode toolchain availability, helper compilation, TCC prompt attribution, and private-interface capability.

## 5. Actual first-user journey comparison

### 5.1 Reviewed versions and scope

- **Apple Reminders candidate:** exact commit `79b1afb...`, not a later branch state.[^C-COMMIT]
- **redpop/apple-calendar-mcp:** tagged release `v0.2.1`, published 2026-08-02, with a release helper archive.[^R-REL]
- **mightymattys/apple-productivity-mcp:** tagged release `v0.1.1`, published 2026-03-27.[^M-REL]
- **Google Calendar bar:** current official OpenAI Google Calendar plugin listing and OpenAI's current connected-app permission controls.[^G-LISTING][^G-DATA][^G-ADMIN]

### 5.2 Five-minute journey

#### Apple Reminders 0.3 candidate

1. The intended tagged flow adds the GitHub marketplace and plugin, then requires a new Codex task.[^C-README]
2. The machine must resolve Python 3.11+ and have Xcode command-line tools while helpers are source-distributed.[^C-README][^C-MCP]
3. Normal first use attempts bounded Core work; when permission is needed, the plugin exposes an explicit access request and macOS may prompt for the app running Codex.[^C-README]
4. Native Extension capability is discovered per operation rather than assumed from macOS version.[^C-README][^C-ARCH]

**Audit judgment:** credible but not yet proven as a repeatable five-minute experience on a clean machine. It has the strongest safety semantics of the local candidates, but also the most environment and support variables.

#### redpop/apple-calendar-mcp v0.2.1

1. The documented install is one `npx`-backed MCP command; the signed helper ships with the package, so users do not need Xcode, a Swift toolchain, or a repository clone.[^R-README]
2. The first calendar request prompts under the helper's own name. The user must then confirm **Full Access** in the nested Calendar settings, not merely “Add Only.”[^R-README][^R-TROUBLE]
3. The helper is installed at a fixed path so the grant can survive package updates; released helpers are Developer ID signed and universal for Apple Silicon and Intel.[^R-ARCH]

**Audit judgment:** strongest five-minute local onboarding and most legible permission identity of the reviewed community projects. Its main first-use trap is the hidden Full Access option, and its private TCC responsibility mechanism creates a platform-compatibility support surface.[^R-TROUBLE][^R-ARCH]

#### mightymattys/apple-productivity-mcp v0.1.1

1. The documented flow requires cloning the repository, running a local install script, manually enabling Calendar and Reminders permissions, and opening the repository in Codex.[^M-README]
2. The install script rewrites repository-specific absolute MCP paths in the checkout and generates local configuration.[^M-INSTALL][^M-MCP]
3. The published MCP and CLI smoke scripts contain author-specific absolute paths and concrete local calendar/list assumptions.[^M-SMOKE-MCP][^M-SMOKE-CLI]

**Audit judgment:** useful reference for plugin/MCP separation and cleanup intent, but the current tagged first-user journey has the highest reproducibility and support burden. The hard-coded smoke paths weaken its value as clean-install evidence; this is a concrete portability finding, not a tool-count judgment.

#### Official OpenAI Google Calendar product bar

1. Google Calendar is discoverable as an OpenAI-made plugin with an “Add plugin” flow and clear schedule/availability use cases.[^G-LISTING]
2. Users connect a selected Google account and grant permission through OAuth; access is limited by that account and approved scopes, and users can disconnect it in settings.[^G-DATA]
3. Managed workspaces can control app access, actions, roles, and when approval is requested.[^G-ADMIN]

**Inference:** the user-visible bar is directory discovery, recognizable account authorization, no user-managed Python/compiler/TCC dependency, cross-surface availability, and centralized controls. This is not an instruction to copy Google's hosted architecture, because native Apple Reminders has a different local data boundary. It is the product bar for making setup and permission state obvious.

### 5.3 Head-to-head evaluation

Ratings are audit judgments, not repository facts. `Strong` means the reviewed evidence supports a low-friction or well-controlled experience; `Mixed` means meaningful strengths with material unresolved risk; `Weak` means a concrete first-user or reproducibility burden.

| Dimension | Apple Reminders candidate | redpop Calendar v0.2.1 | mightymattys v0.1.1 | Official Google Calendar bar |
|---|---|---|---|---|
| First five minutes | **Mixed:** repo install is compact, but Python, Xcode CLT, helper build, new task, and TCC remain.[^C-README][^C-MCP] | **Strong for local:** one package command, signed helper, no source toolchain.[^R-README] | **Weak:** clone, rewrite script, permissions, repo context.[^M-README][^M-INSTALL] | **Strong:** directory listing plus account connection.[^G-LISTING][^G-DATA] |
| Permission UX | **Mixed:** explicit access tool and conservative retry, but clean prompt attribution is not yet evidenced.[^C-README][^C-ISSUE2] | **Strong/Mixed:** helper-named prompt; hidden Full Access option is a known trap.[^R-README][^R-TROUBLE] | **Weak/Mixed:** permission belongs to the app running Codex; setup is largely manual.[^M-README] | **Strong:** named account/OAuth scopes, disconnect, admin controls.[^G-DATA][^G-ADMIN] |
| Reliability semantics | **Strong:** exact identity, idempotency, stale-reference rejection, exact read-back receipts.[^C-SPEC][^C-ARCH] | **Mixed/Strong:** fixed helper lifecycle, signing, recurrence scope; reviewed user docs do not evidence the candidate's receipt/idempotency contract.[^R-ARCH] | **Mixed:** smoke intent exists, but tagged scripts are not portable as published.[^M-SMOKE-MCP][^M-SMOKE-CLI] | **Strong user bar:** official product and account controls; no backend SLA inference is made.[^G-LISTING][^G-ADMIN] |
| Trust/privacy | **Strong disclosure:** local endpoint boundary, host-service caveat, private API risks, verification limits.[^C-PRIVACY] | **Strong local claim:** no network code; private TCC mechanism disclosed.[^R-README][^R-ARCH] | **Mixed:** local architecture is clear, but privacy/support evidence is thinner in the reviewed release docs.[^M-README] | **Strong control model:** account-scoped permissions, disconnect, workspace action controls.[^G-DATA][^G-ADMIN] |
| Support burden | **High:** Python/Xcode/private interfaces/macOS and Reminders combinations.[^C-README][^C-SUPPORT] | **Medium:** Node, nested Full Access setting, signing/TCC edge cases.[^R-README][^R-TROUBLE] | **High:** checkout mutation, absolute paths, environment-specific smoke assumptions.[^M-INSTALL][^M-SMOKE-MCP] | **Lower local burden:** hosted account connection and centralized controls; service support remains OpenAI/Google-specific.[^G-DATA][^G-ADMIN] |
| Demoability | **High after P0:** exact writes, stale-write rejection, sections, images, URLs, and cleanup are visually differentiated.[^C-PR3][^C-ISSUE2] | **High:** one-line setup, free-time workflow, named permission helper.[^R-README] | **Medium:** broad Calendar/Reminders scope, but setup can dominate the demo.[^M-README] | **High:** familiar service, immediately legible schedule use cases and sample prompts.[^G-LISTING] |

### 5.4 What to learn without copying the wrong architecture

- From **redpop**, learn compiler-free distribution, a stable helper identity, a permission-specific first-use explanation, and explicit troubleshooting for TCC states.[^R-README][^R-TROUBLE] Do not import its private responsibility mechanism without a Reminders-specific compatibility and failure study.
- From **mightymattys**, retain the separation between installable plugin UX and shared MCP infrastructure, but do not require mutable absolute-path rewriting or publish machine-specific smoke scripts.[^M-REL][^M-INSTALL][^M-SMOKE-MCP]
- From **Google Calendar**, learn discoverability, named authorization, reversibility, clear task-level prompts, and admin/action controls.[^G-LISTING][^G-DATA][^G-ADMIN] Do not pretend a hosted calendar connector and a local native Reminders store have interchangeable trust boundaries.
- For **Apple Reminders**, preserve exact identity, conservative receipts, Core/Native isolation, and failure-triggered diagnosis. Simplifying setup must not remove the safety semantics that distinguish this project.[^C-ARCH][^C-SPEC]

## 6. Prioritized task list

### P0 — beta blockers

These are release blockers. Universal Plugins Directory eligibility is deliberately **not** a P0 blocker because the current architecture is not eligible and the repo-marketplace beta has independent value.

#### P0.1 — prove the exact clean marketplace launch path

**Exit evidence:**

- Use the exact release candidate or final tag, not a developer checkout with pre-existing state.
- Install through the documented Git-backed marketplace flow.
- Record commit SHA, package SHA-256, marketplace source/ref, `sys.executable`, Python version, macOS version/build, Reminders version/build, architecture, `clang`/Xcode CLT state, and helper build/cache result.
- Start a genuinely new Codex task and record the exact 13-tool list.
- Exercise permission-required → explicit access request → one retry.
- Complete a synthetic create/read/change/delete Core workflow and exact cleanup.
- Exercise one bounded Native Extension workflow if the 13-tool release remains the chosen surface.
- Remove the plugin and inspect only plugin-owned support paths.

This closes the candidate's own still-open clean-Mac gates.[^C-ISSUE2]

#### P0.2 — persist a privacy-safe first-install evidence record

Create a durable, content-free record tied to the exact commit and artifact. It should contain statuses, tool names, error codes, timings, environment versions, prompt attribution, cleanup result, and hashes—not reminder titles, notes, URLs, account identifiers, screenshots of real data, home-directory paths, databases, caches, journals, or backups.[^C-PRIVACY][^C-SUPPORT]

The record must distinguish:

- observed on the clean environment;
- inherited from CI;
- owner-attested from a different machine;
- not tested;
- failed but safely contained.

#### P0.3 — create one final release identity

After owner review:

1. merge the intended candidate;
2. build the deterministic ZIP twice from the exact merged SHA;
3. compare byte-for-byte and audit members;
4. publish the checksum;
5. create `v0.3.0` at the exact reviewed SHA;
6. publish matching release notes and artifact;
7. install from the tag in a new task and verify exactly 13 tools.

These steps are already recorded as open publication gates; this audit does not perform them.[^C-ISSUE2]

#### P0.4 — choose the release surface if Native clean evidence fails

**Owner decision required.** If Core passes on a clean machine but one or more Native Extension operations cannot produce the documented evidence on the declared supported combination, choose explicitly between:

- delaying the 13-tool beta; or
- a separately reviewed Core-plus-Diagnostics beta with honest migration notes.

Do not silently advertise four Native tools as a stable first-user promise without one supported clean-environment run.

### P1 — launch and career polish

1. **Five-minute install card.** Reduce the public path to prerequisites, two install commands, “start a new task,” one first prompt, expected permission dialog, and one recovery link. Validate the card with a person who did not build the plugin.
2. **Permission UX artifact.** Capture a synthetic, redacted sequence showing which process appears in macOS settings, what access is needed for Core, what additional Automation/native state may appear, and how denial is recovered.
3. **90–120 second demo.** Show installation evidence briefly, then spend most of the time on a meaningful Reminder workflow, not the tool list.
4. **Release evidence index.** Link the exact commit, CI, package checksum, clean-install record, tool list, live workflow, limitations, and support policy from one stable page.
5. **Known-good matrix.** Record tested combinations separately from nominal requirements. Avoid implying that one macOS version proves every private Reminders schema.
6. **Support triage contract.** Publish a minimal decision tree for unsupported Python, missing CLT, permission denied, permission prompt absent, Native unavailable, stale reference, verification pending, and cleanup incomplete.
7. **Showcase packet.** Prepare the repository, concise project description, screenshots/video, build notes, exact Codex role, technical evidence, and limitation statement. Submit only after P0.
8. **SNS/GitHub launch thread.** Lead with the user problem and engineering trade-offs. Link immutable evidence. State “repo-marketplace public beta,” not “official OpenAI plugin.”
9. **Portfolio page.** Separate product outcomes, architecture, reliability evidence, and owner-attested live evidence. Do not collapse them into one unsupported “production-ready” claim.

### P2 — later architecture

1. **Signed/notarized prebuilt universal helper at a stable path.** Study this as a stable-release direction to remove the compiler requirement and reduce TCC churn; preserve local-first execution and exact read-back semantics.[^R-README][^R-ARCH]
2. **Reduce ambient runtime dependence.** Evaluate a controlled runtime or clearer interpreter resolution so `python3` variability is not the first user failure.
3. **Second macOS/Reminders build validation.** Validate Core and Native on another supported OS/Reminders combination and, if practical, both Apple Silicon and Intel.
4. **Native capability quarantine.** Make every private-interface failure clearly local to the affected capability, with content-free diagnostics and no optimistic fallback.
5. **Upgrade/TCC persistence study.** Compare remove/re-add, source-helper rebuild, version upgrade, and fixed-helper-path behavior without using broad permission resets as a substitute for a new user.
6. **Support-cost telemetry.** Use opt-in, content-free issue/evidence fields rather than collecting Reminder content or opaque diagnostic bundles.
7. **Paired-local architecture research, only if universal-directory strategy justifies it.** A future design could involve a real authenticated local agent paired with a public service, but it would be a distinct product with availability, offline, security, privacy, and support costs. It must not be described as a remote wrapper that somehow accesses a local app without that local component.

## 7. Safe clean-install evidence model

### 7.1 Evidence levels

| Level | Environment | What it proves | What it does not prove |
|---|---|---|---|
| E0 | Same macOS user after manual cleanup | Regression behavior under a known developer environment | Virgin TCC, empty plugin caches/config, first helper build, unknown-user clarity |
| E1 | New macOS user on the same Mac | Fresh home directory, plugin config, support directories, and a substantially cleaner permission journey | Different hardware, OS build, toolchain installation history, Apple ID/iCloud topology |
| E2 | Second physical Mac or erased/reprovisioned supported Mac with a new user | Strong practical beta evidence for first install and TCC on an independent machine | Full supported matrix, every private schema, every Apple account/shared-list topology |
| E3 | Multiple machines/architectures and at least two macOS/Reminders builds | Stable-release compatibility evidence | Future macOS/private-interface compatibility |

**Recommendation:** E1 is the minimum defensible beta gate when a second Mac is unavailable. E2 is preferable before broad SNS promotion. E3 belongs before a “stable” claim.

### 7.2 Clean procedure

1. Freeze exact commit/tag and package checksum before the run.
2. Record whether the environment is E1, E2, or E3.
3. Confirm there is no copied `~/.codex`/plugin configuration and no pre-existing plugin support directory in the new user's home.
4. Follow only the public install instructions.
5. Record the resolved interpreter and native toolchain before first MCP initialization.
6. Start a new task and capture the exact tool discovery result.
7. Attempt a bounded Core request before access is granted.
8. Record the permission-required result, explicit access request, macOS prompt identity, decision, and one retry.
9. Use a uniquely named synthetic list/reminder. Never use personal content.
10. Exercise create idempotency, exact read, one guarded change, completion/reopen, stale-reference rejection, delete, post-delete absence, and exact cleanup.
11. Exercise one representative Native flow—such as a synthetic section or bounded image/URL attachment—only if the clean environment supports it, then remove the artifact.
12. Remove/re-add or upgrade once and record whether permission is preserved.
13. Uninstall, start a new task, and verify the plugin no longer loads. Inspect only plugin-owned support directories; do not remove parent system directories.
14. Redact identifiers, local paths, hashes that can identify user data, and any Reminder content before committing evidence.[^C-README][^C-PRIVACY]

Do not define “clean” as “ran a broad TCC reset on the developer account.” The redpop comparator documents that command-line-helper reset behavior can be blunt for Calendar and can affect every app for that service.[^R-TROUBLE] A new user or independent Mac gives materially better evidence without disturbing unrelated grants.

### 7.3 What this audit cannot reproduce without a new user or Mac

This execution cannot independently prove:

- the first-ever Reminders prompt identity, timing, or window ordering;
- the absence of prior Codex/plugin marketplace caches;
- the first native helper build on an untouched user profile;
- behavior when Python 3.11+ or Xcode CLT is initially absent;
- permission denial followed by recovery in a virgin TCC state;
- whether an upgrade preserves the exact grant;
- Intel behavior or another macOS/Reminders private schema;
- a different Apple ID, iCloud state, shared-list participant topology, or device convergence;
- that “mobile-visible” evidence corresponds to direct observation on another device.

The owner-recorded current-machine live workflow remains valuable, but it is not a substitute for E1/E2 first-install evidence.[^C-PR3][^C-ISSUE2]

## 8. Launch narrative drafts — do not publish yet

These drafts are written for use **after** P0 is complete. Replace bracketed placeholders with the final tag, release URL, checksum, tested environment, and demo URL.

### 8.1 Korean

> **Apple Reminders 0.3 퍼블릭 베타를 공개합니다.**
>
> Apple Reminders 0.3은 macOS의 미리 알림을 Codex에서 읽고 변경하는 로컬 플러그인입니다. 일상적인 목록·조회·생성·수정·완료·삭제는 EventKit을 우선 사용하고, 섹션·태그·이미지·네이티브 URL 첨부처럼 공개 API가 충분하지 않은 기능은 버전 민감한 macOS 인터페이스를 좁은 경계 안에서 다룹니다.
>
> 이 프로젝트의 중심은 도구 수가 아니라 안전한 변경입니다. 표시 이름 대신 정확한 ID를 사용하고, 조회 범위를 제한하며, 생성 멱등성·일회용 참조·오래된 쓰기 차단·최종 읽기 검증을 유지합니다. 결과는 `verified`, `partial_success`, `committed_verification_pending`처럼 확인 범위를 구분해 보고하며, iCloud나 다른 기기까지 확인하지 않은 결과를 과장하지 않습니다.
>
> 현재 배포는 GitHub repo marketplace를 통한 macOS용 퍼블릭 베타입니다. 요구 사항은 macOS 14+, Python 3.11+, 그리고 소스 형태의 네이티브 helper를 배포하는 동안 Xcode Command Line Tools입니다. MCP 서버는 사용자 Mac에서 `stdio`로 실행되며 플러그인 소유의 원격 서버는 없지만, 도구 결과는 Codex 호스트로 전달되므로 “로컬”을 “서비스 경계 밖”과 같은 뜻으로 사용하지 않습니다.
>
> 이 프로젝트는 Apple이 제공하거나 보증하는 통합이 아니며, 현재 OpenAI의 범용 Plugins Directory에 등록된 플러그인도 아닙니다. 고급 Native Extension 기능은 macOS/Reminders 버전에 따라 제한될 수 있습니다.
>
> 설치·검증·제한 사항: [v0.3.0 release]  
> 재현 가능한 SHA-256: `[checksum]`  
> 2분 데모: [demo]  
> 문제 보고 전 개인정보 안내: [support/privacy]

**Factual basis for this draft:** candidate README, architecture, privacy policy, exact manifest, PR #3, and issue #2.[^C-README][^C-ARCH][^C-PRIVACY][^C-MANIFEST][^C-PR3][^C-ISSUE2] The “not in the universal directory” line follows the current OpenAI local-`stdio` submission rule.[^O-LOCAL]

### 8.2 English

> **Introducing the Apple Reminders 0.3 public beta.**
>
> Apple Reminders 0.3 is a local macOS plugin for reading and changing the current user's Reminders from Codex. Routine list, read, create, update, complete, and delete work prefers EventKit. Features that public APIs do not fully expose—such as sections, tags, images, and native URL attachments—stay behind a narrower, version-sensitive macOS boundary.
>
> The product is designed around safe changes rather than tool count. It uses exact identifiers instead of display names, bounded reads, create idempotency, one-use references, stale-write rejection, and final read-back verification. Receipts distinguish verified, partial, pending, unchanged, and failed outcomes without claiming iCloud or device visibility that was not directly observed.
>
> The current distribution is a macOS public beta through a GitHub repo marketplace. Requirements are macOS 14+, Python 3.11+, and Xcode Command Line Tools while native helpers are distributed as source. The MCP server runs locally over `stdio` and has no plugin-owned remote endpoint, but tool results return to the Codex host, so “local” is not presented as “outside the service boundary.”
>
> This is not an Apple-supported integration and it is not currently listed in OpenAI's universal Plugins Directory. Advanced Native Extension capabilities can vary by macOS and Reminders version.
>
> Install, evidence, and limitations: [v0.3.0 release]  
> Reproducible SHA-256: `[checksum]`  
> Two-minute demo: [demo]  
> Privacy-safe support guide: [support/privacy]

**Factual basis for this draft:** candidate README, architecture, privacy policy, exact manifest, PR #3, and issue #2.[^C-README][^C-ARCH][^C-PRIVACY][^C-MANIFEST][^C-PR3][^C-ISSUE2] The directory statement follows OpenAI's current local-`stdio` rule.[^O-LOCAL]

## 9. Demo outline

**Target length:** 90–120 seconds.  
**Rule:** use a synthetic list, synthetic text, a non-sensitive generated image, and exact cleanup. Show no personal accounts, lists, reminders, URLs, home paths, databases, caches, or logs.

1. **0:00–0:10 — truthful frame**
   - “Local macOS repo-marketplace beta; not an official directory listing.”
   - Show final tag, commit, checksum, and tested environment.
2. **0:10–0:25 — install proof**
   - Show the two documented marketplace/plugin commands.
   - Start a new Codex task.
   - Flash resolved Python/CLT evidence rather than narrating build internals.
3. **0:25–0:40 — permission UX**
   - Ask for a bounded list read.
   - Show permission-required response, explicit access request, macOS prompt identity, and one retry.
4. **0:40–0:58 — useful Core outcome**
   - Create one synthetic reminder in a disposable list.
   - Replay the idempotency key and show that no duplicate is created.
5. **0:58–1:15 — reliability differentiator**
   - Read twice, mutate with one reference, then show stale/concurrent rejection on the older reference.
   - Complete and reopen with concise receipts.
6. **1:15–1:35 — native differentiation**
   - Add a synthetic section and one generated image or native URL attachment.
   - Show it in Reminders, while wording the evidence as local/native or sync evidence—not direct iPhone proof.
7. **1:35–1:50 — cleanup**
   - Delete the reminder/list by exact identity.
   - Show post-delete `not_found`/absence and zero active synthetic lists.
8. **1:50–2:00 — boundaries**
   - On-screen: macOS 14+, Python 3.11+, current source-helper CLT requirement, private-interface variability, local MCP host boundary, not universal-directory listed.

## 10. Portfolio bullets

Use only after linking the supporting evidence.

- Designed a local-first macOS Codex plugin with a static 13-tool Core / Native Extension / Diagnostics interface for native Apple Reminders workflows.[^C-README][^C-ARCH]
- Implemented exact identity, bounded reads, create idempotency, one-use revision references, stale/concurrent write rejection, and read-back-scoped mutation receipts to prevent optimistic or duplicate writes.[^C-SPEC][^C-ARCH]
- Integrated public EventKit with guarded, version-sensitive native adapters for sections, tags, image attachments, and visible URL attachments while preserving a Core-only operating path when native capabilities are unavailable.[^C-ARCH][^C-PRIVACY]
- Built deterministic source packaging and an exact-head macOS 14 CI matrix across Python 3.11 and 3.12; the candidate CI run completed successfully.[^C-CI]
- Produced first-party candidate evidence for two 353-test local runs, a reproducible 48-file ZIP, exact 13-tool discovery, and a disposable create/read/change/complete/reopen/native/delete workflow with exact cleanup.[^C-PR3][^C-ISSUE2]
- Defined privacy-safe diagnostics, support-data boundaries, and verification language that distinguishes local evidence from iCloud convergence or direct device observation.[^C-PRIVACY][^C-SUPPORT]
- Conducted product benchmarking against two first-party Apple Calendar community projects and the official Google Calendar plugin bar, prioritizing onboarding, permission UX, reliability, trust, support burden, and demoability over raw tool count.

## 11. Evidence checklist

### 11.1 Beta release evidence

- [ ] Final merged commit SHA.
- [ ] `v0.3.0` tag points to that exact SHA.
- [ ] Deterministic ZIP built twice from that SHA and compared byte-for-byte.
- [ ] ZIP member audit and final SHA-256.
- [ ] Exact CI run and both matrix job links.
- [ ] E1 or E2 clean-install environment classification.
- [ ] Marketplace source/ref and install commands used verbatim.
- [ ] Resolved Python, macOS, Reminders, architecture, CLT, and helper build state.
- [ ] Exact 13-tool discovery from a new task.
- [ ] First permission-required result, prompt identity, grant/deny choice, and recovery.
- [ ] Synthetic Core workflow with idempotent replay, stale rejection, final reads, and exact cleanup.
- [ ] Representative Native workflow or an explicit, reviewed waiver/surface change.
- [ ] Remove/re-add or upgrade permission behavior.
- [ ] Uninstall and plugin-owned support-directory inspection.
- [ ] No personal Reminder data, local paths, databases, caches, journals, backups, or account IDs in evidence.

### 11.2 Launch/career evidence

- [ ] One-page release evidence index.
- [ ] 90–120 second redacted demo.
- [ ] One clean installation screenshot or short screen recording.
- [ ] Architecture diagram that distinguishes Codex host, local MCP, EventKit, Native Extension, Apple/iCloud, and optional device sync.
- [ ] Known-good environment matrix.
- [ ] Limitations and support window.
- [ ] Korean launch copy reviewed.
- [ ] English launch copy reviewed.
- [ ] Portfolio bullets linked to immutable evidence.
- [ ] GitHub repository profile/social preview does not imply Apple or OpenAI endorsement.
- [ ] Every public “verified” claim names what was actually observed.

### 11.3 OpenAI Showcase packet

OpenAI's Community page confirms a project/demo/workflow submission route, but this audit did not retrieve or submit the current form.[^O-COMMUNITY] The following fields are an **inference from current Showcase entries**, not a promise about the form:

- [ ] Project title and author identity.
- [ ] One-sentence description.
- [ ] Public GitHub URL.
- [ ] Demo/video or live link if available.
- [ ] Screenshot with synthetic data.
- [ ] Build notes and initial problem statement.
- [ ] Iteration story and final deliverable.
- [ ] Exact OpenAI product used, such as Codex.
- [ ] Models, tech stack, use case, harness, and type where applicable.
- [ ] Privacy/local-first boundary.
- [ ] Explicit statement that Showcase submission is separate from Plugins Directory eligibility.

Current Showcase entries visibly present combinations of author, description, GitHub/live links, build notes, products, models, tech stack, use case, harness, and type.[^O-CODEX101][^O-SWIFTY][^O-WAVEFORM]

## 12. Owner decision checklist

- [ ] Is E1 (new macOS user on the same Mac) sufficient for the beta, or is E2 (second/reprovisioned Mac) mandatory?
- [ ] If Native Extension clean evidence fails, delay 0.3 or release a separately reviewed Core-plus-Diagnostics surface?
- [ ] Which exact macOS versions, architectures, Python versions, and Reminders builds will be called “tested,” versus merely “required”?
- [ ] How long will the owner support beta installation failures and private-interface regressions?
- [ ] Is the public developer identity “Soo,” “Oscar-V4,” or another consistent name across GitHub, manifest, Showcase, and portfolio?
- [ ] Will the first launch prioritize developer credibility, early-user utility, or broad install volume?
- [ ] Submit to Showcase immediately after P0, or after one or more external users produce clean evidence?
- [ ] Post a technical forum case study, a user-facing launch, or both?
- [ ] Invest in a signed/prebuilt helper before 0.4 or defer it to the stable milestone?
- [ ] Is official universal-directory reach strategically important enough to fund a distinct paired-local architecture, without weakening local-first privacy?
- [ ] Which screenshots, videos, and logs are acceptable under the privacy policy?
- [ ] Who owns release rollback, support triage, and compatibility decisions after macOS/Reminders updates?

## 13. Final audit verdict

**Human product:** credible and unusually disciplined for a local macOS beta. Its career value comes from the combination of product judgment, local-platform integration, conservative write semantics, deterministic release engineering, and explicit trust limits—not from the number of tools.

**Beta publication:** conditional GO after P0. The candidate's own release ledger already identifies the correct remaining gates: clean marketplace first use and final immutable release identity.[^C-ISSUE2]

**Universal Plugins Directory:** **NO for the current local-`stdio` product; NOT YET until OpenAI supports local MCP submission or a truthful distinct paired architecture exists.**[^O-LOCAL][^O-MCP]

**Official community Showcase:** **feasible as a separate project submission after P0 and demo evidence; acceptance is not guaranteed.**[^O-COMMUNITY][^O-SHOWCASE]

**SNS/GitHub career proof:** **feasible and recommended after P0**, provided every claim links to immutable evidence and the launch never implies Apple or OpenAI endorsement.[^O-GUIDELINES]

---

## Primary sources

### Official OpenAI

[^O-LOCAL]: OpenAI, [Submit your Claude Code plugin to OpenAI](https://developers.openai.com/plugins/guides/submit-claude-plugin) — current guidance for local `stdio` MCP, `.mcpb`, remote MCP, and local-execution cases.
[^O-MCP]: OpenAI, [Build an MCP server](https://developers.openai.com/plugins/build/mcp-server) — stable public HTTPS/streamable HTTP requirements and limits of local endpoints/tunnels for public submission.
[^O-SUBMIT]: OpenAI, [Submit plugins](https://developers.openai.com/plugins/deploy/submission) — submission materials, review flow, and universal Plugins Directory publication.
[^O-REVIEW]: OpenAI, [MCP server review requirements](https://developers.openai.com/plugins/deploy/app-review) — publisher verification, public domain, metadata, annotations, and review materials.
[^O-PACKAGE]: OpenAI, [Package your plugin](https://developers.openai.com/plugins/build/plugins) — plugin structure and separation of universal, local, and repo marketplaces.
[^O-ARCH]: OpenAI, [Plugin architecture](https://developers.openai.com/plugins/concepts/plugins) — skills, MCP servers, optional UI, and the shared directory model.
[^O-APPS]: OpenAI Help Center, [Apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt) — workspace availability and custom-app/plugin distinction.
[^O-COMMUNITY]: OpenAI, [Community](https://developers.openai.com/community) — Showcase submission, Developer Forum, programs, and community spaces.
[^O-SHOWCASE]: OpenAI, [Showcase](https://developers.openai.com/showcase) — current Community filter and project catalog.
[^O-CODEX101]: OpenAI Showcase, [Codex 101](https://developers.openai.com/showcase/codex-101) — community entry with author, GitHub/live links, build notes, products, and stack.
[^O-SWIFTY]: OpenAI Showcase, [Swifty Roguelike](https://developers.openai.com/showcase/swifty-roguelike) — native macOS/Swift project entry.
[^O-WAVEFORM]: OpenAI Showcase, [Waveform Studio](https://developers.openai.com/showcase/waveform-studio) — local-first project entry.
[^O-AMBASSADORS]: OpenAI, [Codex Ambassadors](https://developers.openai.com/community/codex-ambassadors) — paused applications and future-cohort profile.
[^O-OSS]: OpenAI, [Codex for Open Source](https://developers.openai.com/community/codex-for-oss) — maintainer eligibility language.
[^O-OSS-TERMS]: OpenAI, [Codex for Open Source Program Terms](https://developers.openai.com/codex/codex-for-oss-terms) — discretionary eligibility and benefits.
[^O-GUIDELINES]: OpenAI, [Plugin guidelines](https://developers.openai.com/plugins/app-guidelines) — accuracy, reliability, privacy, and no implied OpenAI endorsement.

### Apple Reminders candidate — first-party exact evidence

[^C-COMMIT]: Oscar-V4/apple-reminders, [candidate commit `79b1afb...`](https://github.com/Oscar-V4/apple-reminders/commit/79b1afb4384e8837e1a9f0a88b666960ec820c91).
[^C-PR3]: Oscar-V4/apple-reminders, [draft PR #3: Prepare Apple Reminders 0.3 public beta](https://github.com/Oscar-V4/apple-reminders/pull/3).
[^C-ISSUE2]: Oscar-V4/apple-reminders, [issue #2: Release Apple Reminders 0.3 public beta](https://github.com/Oscar-V4/apple-reminders/issues/2).
[^C-CI]: Oscar-V4/apple-reminders, [exact-candidate GitHub Actions run 32862864357](https://github.com/Oscar-V4/apple-reminders/actions/runs/32862864357).
[^C-README]: Oscar-V4/apple-reminders, [README at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/README.md).
[^C-SPEC]: Oscar-V4/apple-reminders, [0.3 public-beta specification at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/docs/public-beta-0.3.md).
[^C-ARCH]: Oscar-V4/apple-reminders, [architecture at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/docs/architecture.md).
[^C-MARKET]: Oscar-V4/apple-reminders, [repo marketplace at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/.agents/plugins/marketplace.json).
[^C-MANIFEST]: Oscar-V4/apple-reminders, [plugin manifest at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/plugins/apple-reminders/.codex-plugin/plugin.json).
[^C-MCP]: Oscar-V4/apple-reminders, [local MCP declaration at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/plugins/apple-reminders/.mcp.json).
[^C-PRIVACY]: Oscar-V4/apple-reminders, [privacy policy at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/PRIVACY.md).
[^C-SUPPORT]: Oscar-V4/apple-reminders, [support scope at the immutable candidate](https://github.com/Oscar-V4/apple-reminders/blob/79b1afb4384e8837e1a9f0a88b666960ec820c91/SUPPORT.md).

### redpop/apple-calendar-mcp — first-party tagged evidence

[^R-REL]: redpop/apple-calendar-mcp, [release v0.2.1](https://github.com/redpop/apple-calendar-mcp/releases/tag/v0.2.1).
[^R-README]: redpop/apple-calendar-mcp, [README at v0.2.1](https://github.com/redpop/apple-calendar-mcp/blob/v0.2.1/README.md).
[^R-ARCH]: redpop/apple-calendar-mcp, [architecture at v0.2.1](https://github.com/redpop/apple-calendar-mcp/blob/v0.2.1/docs/architecture.md).
[^R-TROUBLE]: redpop/apple-calendar-mcp, [troubleshooting at v0.2.1](https://github.com/redpop/apple-calendar-mcp/blob/v0.2.1/docs/troubleshooting.md).

### mightymattys/apple-productivity-mcp — first-party tagged evidence

[^M-REL]: mightymattys/apple-productivity-mcp, [release v0.1.1](https://github.com/mightymattys/apple-productivity-mcp/releases/tag/v0.1.1).
[^M-README]: mightymattys/apple-productivity-mcp, [README at v0.1.1](https://github.com/mightymattys/apple-productivity-mcp/blob/v0.1.1/README.md).
[^M-INSTALL]: mightymattys/apple-productivity-mcp, [install script at v0.1.1](https://github.com/mightymattys/apple-productivity-mcp/blob/v0.1.1/scripts/install_local_plugins.py).
[^M-MCP]: mightymattys/apple-productivity-mcp, [Apple Reminders MCP declaration at v0.1.1](https://github.com/mightymattys/apple-productivity-mcp/blob/v0.1.1/plugins/apple-reminders/.mcp.json).
[^M-SMOKE-MCP]: mightymattys/apple-productivity-mcp, [MCP smoke script at v0.1.1](https://github.com/mightymattys/apple-productivity-mcp/blob/v0.1.1/scripts/smoke_test_apple_mcp.py).
[^M-SMOKE-CLI]: mightymattys/apple-productivity-mcp, [CLI smoke script at v0.1.1](https://github.com/mightymattys/apple-productivity-mcp/blob/v0.1.1/scripts/smoke_test_apple_cli.py).

### Official Google Calendar product bar

[^G-LISTING]: OpenAI, [Google Calendar plugin listing](https://openai.com/business/plugins/google-calendar/) — OpenAI-made listing, use cases, and sample prompts.
[^G-DATA]: OpenAI Help Center, [Google App for ChatGPT — Data Controls FAQ](https://help.openai.com/en/articles/10408842-google-app-for-chatgpt-data-controls-faq) — account selection, OAuth scope limits, disconnect, and data controls.
[^G-ADMIN]: OpenAI Help Center, [Admin controls, security, and compliance for plugins and apps](https://help.openai.com/en/articles/11509118-admin-controls-security-and-compliance-for-plugins-and-apps) — role, access, action, and permission controls.
