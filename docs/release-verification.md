# Release verification and authenticity

GitHub Release assets are not trusted by filename or by `SHA256SUMS` alone.
The canonical path is the repository verifier, which downloads the two assets
again and independently binds them to the exact tag, immutable release, source,
workflow, and signed helper history.

## Canonical command

Use GitHub CLI 2.92 or newer and an exact, clean tag checkout:

```bash
git clone https://github.com/Oscar-V4/apple-reminders.git
cd apple-reminders
git switch --detach vX.Y.Z
python3 scripts/verify_release_assets.py vX.Y.Z
```

The command does not access Apple Reminders or local Reminder data. It:

1. proves the local and remote tag object agree, resolves the exact tag commit,
   and requires that commit to be an ancestor of current canonical
   `https://github.com/Oscar-V4/apple-reminders.git` `main`; the checkout's
   mutable `origin` is never trusted for source identity;
2. downloads only `apple-reminders-X.Y.Z.zip` and `SHA256SUMS`, rejects any
   other inventory, and checks both GitHub asset digests and the exact checksum
   statement;
3. runs `gh release verify` and requires a GitHub immutable-release attestation
   for the exact tag, commit, two asset names, and two SHA-256 digests;
4. runs `gh attestation verify` separately for the ZIP and `SHA256SUMS`, while
   enforcing the `release.yml` signer workflow, its exact tag commit and ref,
   GitHub-hosted runner identity, SLSA provenance predicate, and one shared
   two-subject statement;
5. runs the strict source/package audit, rebuilds the deterministic ZIP twice,
   and byte-compares both rebuilds with the downloaded ZIP; and
6. proves the signed helper manifest's source and trusted workflow commits are
   ancestors of both the tag and current main, then independently verifies the
   helper manifest attestation and its closed three-subject inventory.

`SHA256SUMS` remains useful for ordinary corruption detection, but it is not a
detached signature. Its authenticity comes from being a separately verified
SLSA subject and an immutable-release asset.

## Publication boundary

The tag workflow keeps four permission domains separate:

- source, helper, and deterministic-package verification has read-only content
  and attestation access;
- a no-checkout job receives OIDC and attestation write permission, rehashes the
  exact two-file payload, re-resolves the tag and workflow identity, and emits
  one SLSA statement for the ZIP and checksum file;
- only the publication job has `contents: write`; it downloads the immutable
  run artifact again, verifies both SLSA lookups and their shared statement,
  requires immutable releases to be enabled, and rechecks tag object, peeled
  commit, inventory, and digests immediately before `gh release create`; and
- a final read-only macOS job downloads the published assets and runs the
  canonical command above. GitHub CLI internally stages attached assets on a
  draft before publishing when release immutability is enabled.

Inter-job release artifacts are run-and-attempt scoped, immutable in the
Actions artifact service, uncompressed, and retained for two days. Signed
helper review artifacts remain available for 14 days. The repository-wide
artifact/log ceiling is 90 days; final published assets instead rely on GitHub
Release immutability and both release and artifact attestations.

## Why tags are not separately signed here

GitHub supports GPG, SSH, or S/MIME tag signatures, but each requires a
maintainer-controlled private signing key and an independent public-key trust
distribution path. Existing release tags are annotated but unsigned, and this
hardening intentionally adds no long-lived signing secret to Actions. Therefore
`git tag -v` is not the canonical authenticity check.

The repository's active `v*` ruleset already rejects tag update and deletion.
For future publications, immutable releases additionally lock the tag and
assets after publication and GitHub signs a release attestation over the tag,
commit, and exact asset digests. The repository-generated SLSA attestations add
the exact `release.yml` workflow identity. Together those are the no-new-secret
authenticity path.

## Node 24 migration for the provenance-bound helper workflow

The seven remaining artifact-action v4 pins are confined to
`prepare-signed-helper-source.yml`: four upload and three download references.
GitHub-hosted runners currently force their JavaScript bundles onto Node 24,
but the pinned action revisions still declare Node 20 and must be retired before
runner removal completes.

A direct edit is unsafe. `eventkit-helper-build.json` records both the SHA-256
of `prepare-signed-helper-source.yml` and a default-branch `workflow_commit`.
The trusted workflow also rejects a selected source branch that changes its own
definition. Updating the seven pins without refreshing the helper would make
the committed manifest stale; asking the old workflow to sign that branch
would fail its own trust gate.

The catch-22 is resolvable only as two reviewed pull requests:

1. **Infrastructure PR:** add a distinct, CODEOWNED bootstrap workflow on the
   protected default branch. It must preserve the existing separation of
   target-code execution, signing secrets, post-sign verification, and the
   final no-checkout OIDC boundary. It may authorize only the reviewed seven-pin
   transition in the target `prepare-signed-helper-source.yml`. Merge this PR
   before dispatch; do not modify any current helper build input in it.
2. **Source/helper refresh PR:** change the four v4 upload pins and three v4
   download pins to the reviewed Node 24 commits, dispatch the already-merged
   bootstrap workflow against that exact branch head, and atomically commit the
   resulting expanded signed helper and provenance manifest with the source
   change. The new manifest must record the target `source_commit`, the trusted
   bootstrap `workflow_commit`, new build-input hashes, and an attestation over
   the notarized ZIP, manifest, and helper checksum inventory. Only after this
   PR passes the complete release verifier may it merge.

The current release-provenance change intentionally performs neither half. It
leaves `prepare-signed-helper-source.yml`, native source and bytes, and
`eventkit-helper-build.json` unchanged, preserving the current helper source
commit `470b2251cae3086d774f23afce30a1e9986ed578` and trusted workflow commit
`1a1181ee919c31a1912b3ea01b5ce0c6054e8e53` for plugin version `0.5.2`.

## Repository policy audit

All checked-in actions are pinned to full 40-character commits. The repository
default `GITHUB_TOKEN` permission is read-only and workflow tokens cannot
approve pull requests. Release jobs grant only their explicit job-level needs.
The repository should also enforce full-SHA action pinning and immutable
releases at the settings layer; the workflow fails closed before publication if
immutability is not enabled.

References: [GitHub artifact attestations](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations),
[GitHub CLI attestation verification](https://cli.github.com/manual/gh_attestation_verify),
[immutable releases](https://docs.github.com/en/enterprise-cloud@latest/code-security/concepts/supply-chain-security/immutable-releases),
[signed tags](https://docs.github.com/en/authentication/managing-commit-signature-verification/signing-tags),
and [Node 20 runner deprecation](https://github.blog/changelog/2025-09-19-deprecation-of-node-20-on-github-actions-runners/).
