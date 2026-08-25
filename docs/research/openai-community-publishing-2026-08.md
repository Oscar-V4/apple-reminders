# OpenAI community and publishing routes for Apple Reminders

Research date: 2026-08-26 (Asia/Seoul)

This note separates three kinds of statements:

- **Confirmed** — stated by a first-party OpenAI, Apple, GitHub, MCP, or
  project-owner source.
- **Unavailable / paused** — a first-party source explicitly rules out the
  route or says it is not currently accepting applications.
- **Inference** — a product or portfolio recommendation derived from those
  sources, not a promise from OpenAI.

## Executive finding

**Confirmed:** OpenAI supports community distribution through public GitHub
repositories and repo marketplaces. It documents Git-backed marketplace
sources such as `owner/repo`, while also making clear that local and repo
marketplaces are separate from the universal Plugins Directory. This repository
already has that repo-marketplace layout and documents the corresponding
install commands. See [Package your
plugin](https://developers.openai.com/plugins/build/plugins) and this
repository's [marketplace manifest](../../.agents/plugins/marketplace.json).

**Confirmed:** The [Showcase submission
form](https://openai.com/form/showcase-submission/) is open. It explicitly
accepts a polished product, creative experiment, or open-source demo built
using OpenAI models; asks whether Codex was used; accepts a public GitHub
repository instead of a hosted demo; and asks for the displayed author name,
cover image, setup steps, and build story.

**Inference:** For a local-first project, Showcase is the strongest currently
documented route to official OpenAI community visibility that does not require
changing the product into a hosted service.

**Unavailable for the current architecture:** The local `stdio` MCP server
cannot be submitted unchanged as the MCP component of a public universal
plugin. OpenAI requires a public production MCP URL on a publicly accessible
domain and rejects local or testing endpoints. A Secure MCP Tunnel can expose a
private or local `stdio` server in developer mode, but OpenAI explicitly says
that this does not replace the public HTTPS endpoint required for submission.
See [MCP review
requirements](https://developers.openai.com/plugins/deploy/app-review) and
[Connect and test your
plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).

**Inference:** The pragmatic launch is therefore:

1. publish a tagged GitHub Release and repo marketplace;
2. collect clean-install and real-user evidence;
3. submit the released project to the OpenAI Developer Showcase;
4. share the same evidence in the OpenAI Developer Forum and on social media;
5. describe it as a **community-built, open-source Apple Reminders plugin for
   Codex**, not as an official or OpenAI-endorsed plugin.

Building hosted infrastructure only to obtain a directory badge would change
the product from a small local utility into an account, relay, privacy, and
operations service. That is a product-direction decision, not release hygiene.

## 1. Universal ChatGPT and Codex Plugins Directory

### Confirmed official path

OpenAI's current plugin model has one directory shared by ChatGPT and Codex. A
public plugin may contain skills, an MCP server, or both, and individual
capabilities may still be surface-specific. OpenAI also recommends starting
with the smallest shape that supports the use case. See [Plugin
architecture](https://developers.openai.com/plugins/concepts/plugins).

The official publishing flow is:

1. obtain `Apps Management: Write` access in the publishing OpenAI Platform
   organization;
2. verify the individual or business identity that will be displayed as the
   publisher;
3. prepare the listing, support, website, privacy, and terms URLs;
4. for an MCP submission, provide and verify a public production MCP domain;
5. provide accurate tool schemas and `readOnlyHint`, `openWorldHint`, and
   `destructiveHint` annotations;
6. provide at least five positive and three negative reviewer test cases;
7. submit for review;
8. after approval, choose when to publish.

Only after the final publish step does the listing appear in the universal
directory. Review does not begin or complete merely because a repository is
public. See [Submit
plugins](https://developers.openai.com/plugins/deploy/submission).

The same guide requires public publisher-facing materials: plugin name and
descriptions, logo, category, website, support URL, privacy policy, terms,
country availability, and release notes. It also requires MCP responses to
exclude unnecessary personal data, secrets, debug data, and internal
identifiers.

OpenAI's [Plugin
guidelines](https://developers.openai.com/plugins/app-guidelines) add an
important communication boundary: plugins must not imply they are made or
endorsed by OpenAI, must be stable and complete rather than trials or demos,
and must use clear, accurate names and descriptions. Approval may lead to
directory availability, but enhanced placement or proactive suggestions are
not promised.

### Current local-`stdio` limitation

OpenAI's production MCP guidance requires a stable HTTPS endpoint using
streamable HTTP. The review checklist says the server must be hosted on a
publicly accessible domain and must not be a local or testing endpoint. See
[MCP server](https://developers.openai.com/plugins/concepts/mcp-server) and
[MCP review
requirements](https://developers.openai.com/plugins/deploy/app-review).

The developer-mode guide does permit a Secure MCP Tunnel to reach a private
`stdio` or HTTP server for testing. It immediately distinguishes that from
submission: tunnels and development forwarding services do not replace the
public production endpoint. See [Connect and test your
plugin](https://developers.openai.com/plugins/deploy/connect-chatgpt).

Apple independently requires the app or process using EventKit to obtain the
person's permission before accessing the local calendar/reminder database;
full access is required to read and write reminders. See [Accessing the event
store](https://developer.apple.com/documentation/eventkit/accessing-the-event-store)
and [`requestFullAccessToReminders`](https://developer.apple.com/documentation/eventkit/ekeventstore/requestfullaccesstoreminders%28completion%3A%29).

**Inference:** A normal hosted MCP server cannot directly inherit each user's
macOS EventKit permission or open that user's local reminder store. A hosted
version would need a separately installed local agent or another Apple-backed
account/data architecture, user authentication, per-user routing, and new
privacy and operational controls.

### Skills-only is not a confirmed shortcut

**Confirmed:** Public skills-only plugins are supported, skills may include
supporting scripts and resources, and surface-specific capabilities are
allowed. See [Plugin
architecture](https://developers.openai.com/plugins/concepts/plugins) and
[Submit plugins](https://developers.openai.com/plugins/deploy/submission).

**Inference / unverified:** A separate Codex-oriented skills-only edition
might eventually call tools already present on a user's Mac. The current skills,
however, are designed around this plugin's MCP contract. OpenAI's public docs do
not establish that dropping the MCP and depending on arbitrary local script
execution would provide the same install and execution behavior across public
plugin surfaces or pass review. Treat this as a prototype requiring portal
clarification and a new safety evaluation, not as a release workaround.

## 2. Public distribution that works now

### GitHub repo marketplace

**Confirmed:** OpenAI documents public Git repositories as marketplace sources:

```sh
codex plugin marketplace add owner/repo
codex plugin marketplace add owner/repo --ref main
```

It describes local and repo marketplaces as authoring, testing, and team
distribution sources separate from the universal directory. See [Package your
plugin](https://developers.openai.com/plugins/build/plugins).

Once `v0.3.0` exists, this repository's intended pinned install is therefore a
valid community distribution route:

```sh
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.3.0
codex plugin add apple-reminders@oscar-v4-reminders
```

This is public and installable, but it must be described as a GitHub/repo
marketplace release, not as an OpenAI directory listing.

### Official MCP Registry as an optional later route

The [official MCP
Registry](https://modelcontextprotocol.io/registry/about) is a preview,
community-driven MCP metadata registry. It points to public packages or remote
servers and is intended for discovery by downstream MCP clients and
marketplaces. It is distinct from OpenAI's universal Plugins Directory.

The registry supports local `stdio` packages published through public npm,
PyPI, NuGet, OCI, or MCPB artifacts; MCPB artifacts may be hosted in GitHub
Releases with a SHA-256 recorded in `server.json`. See [supported package
types](https://modelcontextprotocol.io/registry/package-types).

**Inference:** MCP Registry publication could broaden generic MCP-client
discovery later, but it adds another package and release contract. It should be
undertaken only if demand exists outside Codex; it is not required for the
initial Codex community release and would not create an OpenAI directory
listing.

## 3. Official OpenAI community routes

### Developer Showcase — open now

**Confirmed:** The [OpenAI community
page](https://developers.openai.com/community) directs builders to submit a
project, demo, or workflow to the Developer Showcase. The [submission
form](https://openai.com/form/showcase-submission/) specifically requests:

- whether Codex and any other coding agent were used;
- the models, APIs, capability, use cases, and technical stack (with `N/A`
  permitted where appropriate);
- a short account of the build process and how coding agents were used;
- either a public GitHub repository or a hosted URL;
- reproducible setup steps;
- title, tagline, project description, displayed author name, and a public
  cover image.

The form says open-source demos are eligible, so a local macOS plugin does not
need to be converted into a hosted SaaS merely to apply.

The program agreement is equally important: submission grants OpenAI rights to
test and promote the material, but OpenAI does not guarantee publication,
placement, visibility, ranking, or promotion. A submitter may not imply OpenAI
created, certified, supported, or endorsed the project without a separate
written agreement.

**Inference:** Submit after the tagged release and clean-install proof are
public. The Showcase is a better recognition route for this local-first project
than distorting its architecture to satisfy hosted MCP submission.

### Developer Forum — open now

**Confirmed:** The [OpenAI Developer
Community](https://community.openai.com/) has a Codex category for Codex tools
and workflows and a Community category that explicitly welcomes sharing
projects under development. It is the official discussion and feedback route,
not a certification or publication review.

**Inference:** Use two distinct posts rather than one promotional blast:

1. a concise project post with the tagged release, demo, privacy boundary, and
   request for Mac testers;
2. a product-feedback post explaining why local native integrations need a
   reviewed distribution path for `stdio`/device-bound MCP servers.

The second post is the appropriate place to propose a future OpenAI mechanism
without claiming that such a mechanism already exists.

### Codex Ambassadors — paused

**Unavailable / paused:** [Codex
Ambassadors](https://developers.openai.com/community/codex-ambassadors) is not
accepting applications as of the research date. OpenAI says a future cohort is
planned and that expressions of interest are not active applications while the
program is paused.

The future-cohort profile is relevant: OpenAI names community organizers,
open-source maintainers, workshop facilitators, and Codex power users who create
reusable learning assets, run events, and provide real-world feedback. The
program expects roughly two to four hours per week and one contribution per
month.

**Inference:** A maintained plugin, public troubleshooting notes, a tutorial,
and evidence of helping actual users are stronger future Ambassador evidence
than a single launch announcement.

### Codex for Open Source — open, but adoption-oriented

**Confirmed:** [Codex for Open
Source](https://developers.openai.com/community/codex-for-oss) currently accepts
applications from maintainers of active open-source projects. The [application
form](https://openai.com/form/codex-for-oss/) asks for a public GitHub profile
and repository, primary/core maintainer status, stars or downloads, ecosystem
importance, active-maintenance evidence, and intended use of API credits.
Applications are reviewed on a rolling basis; selection is not guaranteed.

**Inference:** The project can technically apply, but its case will be stronger
after external installs, issues, releases, and maintainer activity establish
meaningful use. This program supports maintainers; it is not a plugin listing
or an award application.

### OpenAI Cookbook — possible contribution, not a plugin submission

**Confirmed:** The OpenAI-owned
[`openai-cookbook`](https://github.com/openai/openai-cookbook) accepts community
content contributions on a best-effort basis and does not guarantee whether or
when a contribution will be reviewed or merged. Its scope is useful patterns
and examples for working with the OpenAI platform. See its [contribution
guide](https://github.com/openai/openai-cookbook/blob/main/CONTRIBUTING.md).

**Inference:** The repository itself should not be submitted as a Cookbook PR.
A later, independently useful case study about safe local-first MCP design,
permission-bound macOS automation, idempotent receipts, or real-world plugin
evaluation might fit if it teaches an OpenAI/Codex pattern and meets the
Cookbook's uniqueness bar.

### Social media — useful outreach, not a formal submission

**Confirmed:** OpenAI's community page features public builder posts and links
to the official `@OpenAIDevs` X account, but no first-party source documents a
tag, hashtag, or social post as a formal submission or a guarantee of being
featured. The Showcase form is the documented submission mechanism.

**Inference:** Tagging `@OpenAIDevs` is reasonable outreach after release, but
the post should link durable proof and say “built with Codex” or
“community-built for Codex.” It should not say “official,” “partnered with
OpenAI,” “approved,” or “featured” unless the corresponding directory or
Showcase event has actually occurred.

## 4. Provenance of the Apple Calendar references

The two calendar projects already cited in this repository use different
distribution models. Neither project's owner-controlled GitHub sources, by
themselves, establish an OpenAI Plugins Directory approval.

| Project | Owner-source provenance | Actual distribution route | What it proves |
| --- | --- | --- | --- |
| `redpop/apple-calendar-mcp` | [`v0.2.1`](https://github.com/redpop/apple-calendar-mcp/releases/tag/v0.2.1), published 2026-08-02 from commit [`76eab81`](https://github.com/redpop/apple-calendar-mcp/commit/76eab816a9acc5a7d4275258ebd451c762e9f9d6) | Public npm package invoked with `npx`; official MCP Registry metadata; GitHub Release with `calendar-helper.zip` | A polished generic local MCP distribution with a signed helper; not evidence of an OpenAI universal-plugin listing |
| `mightymattys/apple-productivity-mcp` | [`v0.1.1`](https://github.com/mightymattys/apple-productivity-mcp/releases/tag/v0.1.1), published 2026-03-27 from commit [`6caa7f8`](https://github.com/mightymattys/apple-productivity-mcp/commit/6caa7f896431a853cadad7a2564f9e10e838e332) | Clone/install script plus repo-local Codex marketplace, `.codex-plugin` manifests, and local `.mcp.json`; the release has no binary assets | A Codex plugin/repo-marketplace layout; not evidence of an OpenAI universal-plugin listing |

### `redpop/apple-calendar-mcp`

Its [README at the reviewed
commit](https://github.com/redpop/apple-calendar-mcp/blob/76eab816a9acc5a7d4275258ebd451c762e9f9d6/README.md)
documents `npx -y @redpop/apple-calendar-mcp`, macOS 14 and Node 20
requirements, a signed helper included in the npm package, local-only data flow,
and support for any MCP client that speaks `stdio`. It does not contain a
`.codex-plugin` manifest in the reviewed tree.

Its [`server.json`](https://github.com/redpop/apple-calendar-mcp/blob/76eab816a9acc5a7d4275258ebd451c762e9f9d6/server.json)
declares an npm `stdio` package under
`io.github.redpop/apple-calendar-mcp`. The [official MCP Registry
API](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.redpop%2Fapple-calendar-mcp&limit=10)
lists `0.2.1` as the latest active version. Its [release
workflow](https://github.com/redpop/apple-calendar-mcp/blob/76eab816a9acc5a7d4275258ebd451c762e9f9d6/.github/workflows/release.yml)
builds a universal helper, imports a Developer ID certificate, notarizes and
verifies the artifact, publishes npm with provenance, creates the GitHub
Release, and then publishes registry metadata.

This is strong supply-chain and first-install evidence. It is also a useful
warning against conflating three different things: an MCP Registry entry, a
GitHub/npm release, and an OpenAI universal plugin listing.

### `mightymattys/apple-productivity-mcp`

Its [README at the reviewed
commit](https://github.com/mightymattys/apple-productivity-mcp/blob/6caa7f896431a853cadad7a2564f9e10e838e332/README.md)
documents two installable Codex plugins over a shared local MCP server, a clone
plus `install_local_plugins.py` setup, and direct macOS Calendar/Reminders
permissions. Its [repo marketplace
manifest](https://github.com/mightymattys/apple-productivity-mcp/blob/6caa7f896431a853cadad7a2564f9e10e838e332/.agents/plugins/marketplace.json)
and [Apple Calendar plugin
manifest](https://github.com/mightymattys/apple-productivity-mcp/blob/6caa7f896431a853cadad7a2564f9e10e838e332/plugins/apple-calendar/.codex-plugin/plugin.json)
show that this is a GitHub/repo-marketplace Codex plugin rather than merely a
generic MCP package.

Its `v0.1.1` release notes say the installable plugins were separated from the
shared MCP layer and a generated local MCP configuration was introduced. The
release has no downloadable assets, so its owner-source distribution evidence
is the repository and installer rather than a signed release package.

### Product comparison implication

**Inference:** Apple Reminders does not need to imitate either calendar
project's tool count or runtime. It should match the parts that make a local
native integration feel trustworthy:

- one pinned, documented installation path;
- predictable first-use permission behavior;
- a versioned release artifact and checksum;
- no compiler requirement for ordinary users where practical;
- representative live tests with exact cleanup;
- a clear local-data and private-interface boundary;
- a stable update and uninstall story.

The signed, fixed-path helper in `redpop` is the most material later comparison
for TCC stability and install friction. The repo-marketplace packaging in
`mightymattys` is the closer comparison for the current Codex distribution
shape. Neither project lowers the bar for guarded writes, exact verification,
or privacy merely because its public surface is smaller.

## 5. Evidence that best supports an individual career portfolio

This section is recommendation, not an OpenAI hiring or endorsement policy.

### Strongest durable evidence

1. **A public, tagged release that another person can install.** GitHub Releases
   package software, release notes, and downloadable artifacts in one durable
   page. See [Releasing projects on
   GitHub](https://docs.github.com/en/repositories/releasing-projects-on-github).
2. **A two-minute repository review path.** GitHub's own resume guidance says
   hiring managers may inspect a project only briefly and recommends a concise
   README with features, setup, a demo, testing instructions, understandable
   code, and tests. See [Using your GitHub profile to enhance your
   resume](https://docs.github.com/en/account-and-profile/tutorials/using-your-github-profile-to-enhance-your-resume).
3. **Verifiable quality signals.** Link a passing default-branch CI run and
   expose the status through a README badge; GitHub documents that the badge
   communicates the current workflow status. See [Adding a workflow status
   badge](https://docs.github.com/en/actions/how-tos/monitor-workflows/add-a-status-badge).
4. **Evidence of product judgment.** Preserve public issues, design decisions,
   changelog entries, review findings, performance results, and the reason
   features were deliberately withheld. This shows maintenance and trade-off
   work rather than only generated code.
5. **Evidence from other people.** Real installation reports, bug reports,
   fixes, discussions, forks, or small external contributions are more credible
   than self-reported download numbers. Do not manufacture stars or testimonials.
6. **A public profile that points to the work.** GitHub recommends a professional
   bio, profile README, social/portfolio links, and three to five pinned projects,
   including owned and contributed-to open-source work. See the same [GitHub
   resume guide](https://docs.github.com/en/account-and-profile/tutorials/using-your-github-profile-to-enhance-your-resume).
7. **Official community evidence when actually obtained.** A published OpenAI
   Showcase page or universal directory listing is strong third-party evidence,
   but submission alone is not. Link the accepted page only after it exists.

### A credible launch post

**Inference:** A strong social post should contain:

- one sentence describing the user's problem solved;
- “open-source, local-first Apple Reminders plugin for Codex”;
- a 30–60 second first-install and representative-workflow video;
- the exact tagged release and one-command marketplace install;
- macOS and permission requirements;
- local-data/privacy and private-interface caveats in plain language;
- linked CI/live-smoke evidence rather than an unsupported “production-ready”
  claim;
- a request for a small number of Mac testers and a direct issue-report link;
- a factual “built and audited with Codex” build story;
- optional `@OpenAIDevs` tagging as outreach, without implying endorsement.

### Safe claim language

| Claim | Use when |
| --- | --- |
| “Community-built open-source plugin for Codex” | After the public tagged repo-marketplace release |
| “Built and audited with Codex” | When the public history/report supports the statement |
| “Submitted to the OpenAI Developer Showcase” | Only after submitting; do not imply acceptance |
| “Featured in the OpenAI Developer Showcase” | Only after an OpenAI-hosted project page exists |
| “Published in the ChatGPT and Codex Plugins Directory” | Only after review, approval, and publication produce the live listing |
| “Official,” “OpenAI-endorsed,” or “partnered with OpenAI” | Do not use without explicit written authorization |

## 6. Recommended sequence

### Before the public announcement

1. Finish clean-environment first-install evidence and publish it without user
   reminder content.
2. Merge the release candidate, rebuild the deterministic artifact from the
   merged commit, publish `v0.3.0`, and expose the checksum.
3. Verify the exact tagged marketplace install on a Mac that did not use the
   development checkout.
4. Make the README's first screen answer: what it does, macOS requirement,
   one-command install, permission behavior, local-data boundary, demo, CI, and
   support link.
5. Record one short video using disposable reminders and lists.
6. Pin the repository on the developer's GitHub profile and make the author name
   consistent across the plugin manifest, GitHub profile, Showcase form, and
   social profiles.

### Launch and official community outreach

1. Publish the factual social post and tag `@OpenAIDevs` if desired.
2. Post the release to the Developer Forum's Codex or Community category with a
   concrete request for testers.
3. Submit the same released project to the OpenAI Developer Showcase; do not
   announce it as featured unless accepted.
4. Separately propose a reviewed local/device-bound MCP publication path in the
   forum, citing the public-HTTPS limitation and EventKit permission model.

### After real adoption

1. Convert user reports into issues, fixes, release notes, and regression tests.
2. Apply to Codex for Open Source once there is credible usage and active
   maintenance evidence, or apply earlier only with a clear ecosystem-importance
   case.
3. Monitor the Codex Ambassadors page for the next cohort and build the expected
   evidence through tutorials, support, reusable assets, or meetups.
4. Consider an official MCP Registry package only if non-Codex MCP-client demand
   justifies the extra release surface.
5. Revisit universal-directory architecture only if OpenAI adds a reviewed local
   executable route or users justify the cost and privacy burden of a hosted
   relay plus local agent.

## Bottom line

The project has a legitimate path to public use and career-visible contribution
without pretending that GitHub distribution is an OpenAI directory approval.
The honest and strongest story is: a developer identified real Apple Reminders
failure modes, built a local-first Codex plugin, documented its trust boundary,
tested it against the native app, released it reproducibly, supported real
users, and submitted the finished work through OpenAI's documented community
channels.

That story is stronger than adding cloud infrastructure solely to obtain an
“official” label, and it remains true even if Showcase or directory review is
not granted.
