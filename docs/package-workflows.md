# Go, npm and Rust package workflows

These reusable workflows publish one public root Go module, npm package, or Rust crate. They are for ordinary single-package repositories, not monorepos or workspaces. Maven has its own contract in the [repository README](../README.md).

All callers should pin the shared workflow to a reviewed full commit SHA. Do not use a mutable branch or tag for a workflow that can publish packages. Replace `<shared-workflow-commit>` below with the commit you reviewed.

## Caller workflows

Each caller repository has one read-only pull-request build workflow and one release workflow. The release caller receives pushes for all branches; the reusable workflow itself checks the caller repository's current default branch. This works if the default branch is called `main`, `master`, `prime`, or is renamed later.

`.github/workflows/build.yml` for Go:

```yaml
name: build

on:
  pull_request:

permissions:
  contents: read

concurrency:
  group: build-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  build:
    uses: BonfireTrunk/.github/.github/workflows/go-build.yml@<shared-workflow-commit>
```

For npm, call `npm-build.yml`; it accepts the optional `node-version` input, which defaults to `24`. For Rust, call `rust-build.yml`; it accepts the optional `toolchain` input, which defaults to `stable`. A Rust release accepts only `stable`, `beta`, `nightly`, a dated channel, or a numeric toolchain with a complete `X.Y.Z` version.

`.github/workflows/release.yml` for Go:

```yaml
name: release

on:
  push:
    branches: ['**']
  workflow_dispatch:

permissions: {}

concurrency:
  group: release
  cancel-in-progress: false

jobs:
  release:
    permissions:
      contents: write
    uses: BonfireTrunk/.github/.github/workflows/go-release.yml@<shared-workflow-commit>
    with:
      release-environment: release # default; replace only with another existing environment
```

For npm, use `npm-release.yml` and add `packages: write` and `id-token: write`:

```yaml
    permissions:
      contents: write
      packages: write
      id-token: write
    uses: BonfireTrunk/.github/.github/workflows/npm-release.yml@<shared-workflow-commit>
    with:
      release-environment: release # default; replace only with another existing environment
```

For Rust, use `rust-release.yml` and add `id-token: write`:

```yaml
    permissions:
      contents: write
      id-token: write
    uses: BonfireTrunk/.github/.github/workflows/rust-release.yml@<shared-workflow-commit>
    with:
      release-environment: release # default; replace only with another existing environment
```

Do not add `secrets: inherit`. These workflows are designed to use GitHub's short-lived workflow token and OIDC, not registry write tokens stored in caller repositories.

## Release contract

Merge a release pull request into the repository's default branch with a squash-merge title of one of these forms:

| Title | Effective version and tag |
| :-- | :-- |
| `Release 1.2` | `1.2.0`, `v1.2.0` |
| `Release 1.2.3` | `1.2.3`, `v1.2.3` |
| `Release 1.2.3-rc.1` | `1.2.3-rc.1`, `v1.2.3-rc.1` |

Only the title has the `X.Y` shortcut. The checked-in package manifest and changelog must use the complete effective version. Every release needs one nonempty changelog section exactly like:

```markdown
## [1.2.0] - 2026-11-05

- Explain the user-visible change.
```

The workflow rejects build metadata, slash-separated labels, four-component versions, leading-zero numeric parts, and invalid prerelease identifiers. A suffix such as `-hotfix.1` is valid SemVer but is a prerelease and sorts before `1.2.3`; a stable hotfix after `1.2.3` is normally `1.2.4`. Prereleases create GitHub prereleases and npm's `next` channel. Stable npm releases use `latest`; the workflow refuses to move `latest` backwards.

A manual run is allowed from the current default branch or an existing `v<full-version>` tag. The checked-out commit must still have the matching `Release ...` title and be in the current default branch's history. This is the retry path after an interrupted registry publication. It cannot create a release from an arbitrary feature branch or change the meaning of an existing tag.

## Layout and identity checks

The workflows refuse layouts they cannot publish safely:

| Ecosystem | Required layout and identity |
| :-- | :-- |
| Go | One root `go.mod`, no `go.work`. Its module must be `github.com/<owner>/<repository>` for v0/v1, or the same path ending `/vN` for v2 and later. |
| npm | One root public `package.json`, no `workspaces`, and a committed `package-lock.json`. The package is scoped as `@<lowercase-github-owner>/<name>`, and both root version fields in the lockfile equal the manifest version. Its `repository` is exactly `https://github.com/<owner>/<repository>`, optionally with `.git` or `git+https://...git`; `publishConfig` is absent or exactly `{ "access": "public" }`. |
| Rust | One root `Cargo.toml` with `[package]`, no workspace, and a committed `Cargo.lock`. The manifest permits `crates-io` publishing. Packaged dependencies must resolve from crates.io. Cargo artifact dependencies are unsupported. |

The caller repository must be public. Protect its default branch and release tags so only reviewed changes can reach this workflow. The release job always names a GitHub environment: `release` by default, or the nonempty `release-environment` input. Create and configure that matching environment in every caller repository before publishing. GitHub can automatically create a missing environment without protection, so naming it alone is not an approval gate. Required reviewers and branch/tag restrictions are configured by the repository owner.

## Untrusted pull requests

Use the documented `pull_request` build trigger. Fork PR jobs receive a read-only GitHub token and no repository publishing secrets under GitHub's normal fork policy. These build workflows request neither write permissions nor OIDC credentials. Release jobs reject PR events and run package scripts separately from the job holding publishing credentials. Embedded Python helpers use isolated mode so checkout files and `PYTHONPATH` cannot replace their standard-library imports.

Keep fork workflows restricted to read-only tokens without secrets. Do not execute PR head code from a privileged `pull_request_target` or `workflow_run` job, and do not pass inherited secrets to builds. Approving a fork's test run is not approval to merge or publish its code.

Require review for changes to caller workflows, dependency locks, and build/package scripts. Configure the release environment's reviewers and permitted branches/tags before publishing; merely naming the environment adds no protection. Collaborators with write access and actions running in a publishing job remain trusted. SHA pins prevent silent action updates, but do not prove that the pinned code or a reviewed package is harmless. These workflow files do not audit or enforce the caller repository's GitHub settings.

## Registry setup

### npm

Configure the npm package as a public package and configure a GitHub Actions trusted publisher in npmjs.com. npm's current documentation requires a GitHub-hosted runner, `id-token: write`, Node at least 22.14.0, and npm at least 11.5.1. The release workflow uses Node 24 and npm 11.21.1.

Configure the trusted publisher with the caller repository owner and repository, caller workflow filename `release.yml`, and the exact environment name selected above. npm documents that when `workflow_call` invokes a workflow containing `npm publish`, validation checks the calling workflow's name. Configure `release.yml`, not `npm-release.yml`, before the first automated release. Enable direct `npm publish`; the workflow does not use `npm dist-tag` after publishing.

The release build runs `npm ci`, its configured build script, and tests, then runs `npm pack --ignore-scripts`. The build script must place every distributable generated file in the package before that command. The same tarball is first published to GitHub Packages, then the tag and GitHub release are created, then it is published to npmjs.com with OIDC. The workflow verifies the bytes of an already-published version rather than overwriting it. GitHub Packages needs `packages: write`; npmjs.com needs no stored write token.

### Rust

Publish the initial crate manually with an API token, then configure its trusted publisher in crates.io for the caller GitHub repository, workflow filename `release.yml`, and the same environment name selected above. crates.io's official setup requires the crate to already exist and its GitHub action exchanges an OIDC token for a temporary publishing token. The workflow uses that temporary token, so callers supply `id-token: write` and no `CARGO_REGISTRY_TOKEN` secret.

The release build validates and packages the crate once. The workflow saves that verified `.crate` file in the GitHub release bundle, creates the tag and GitHub release, then uploads those exact bytes with Cargo's documented registry wire format. It checks the registry index checksum before treating a version as complete.

### Go

Go modules have no GitHub Packages publishing step. A semantic Git tag and public repository are the publication mechanism. After creating the tag and GitHub release, the workflow repeatedly asks `proxy.golang.org` for the module using only that proxy and the public checksum database. It reports success only when the proxy returns the expected tag, checksums, and an `Origin.Hash` equal to the release commit. The Go proxy can take time to observe a new tag; retry the release workflow rather than moving the tag.

## Retries and immutable artifacts

Before a release changes a registry, it verifies the release title or retry tag, the commit, manifest, changelog, version, and any existing tag. The build job creates one release bundle containing the notes and, where applicable, the package archive. It passes that one immutable GitHub Actions artifact to the publish job by artifact ID. The GitHub release stores the same bundle with its SHA-256 digest.

Rerunning only the failed publish job uses its original artifact ID. A new dispatch or a rerun of all jobs that begins before a GitHub release bundle exists builds again; if that conflicts with a registry record, it fails rather than replacing the record. Once the GitHub release asset exists, retries restore that exact bundle instead of rebuilding. Every path verifies embedded checksums and fails if an existing registry version or GitHub release asset has different bytes. This preserves the ordering below while allowing safe recovery from transient failures:

| Ecosystem | Publication sequence |
| :-- | :-- |
| npm | Build one tarball, GitHub Packages, Git tag and release with bundle, npmjs.com |
| Rust | Test/package one crate, Git tag and release with bundle, crates.io |
| Go | Test/vet, Git tag and release, verified `proxy.golang.org` availability |

If a registry reports a partial or conflicting version, stop and investigate the registry record. Never delete or retag a published version merely to make a retry pass.

## Maintaining these workflows

Release workflow YAML is generated from `.github/release-tools/`; do not hand-edit the generated release files. Install [actionlint](https://github.com/rhysd/actionlint) on your PATH (validated here with 1.7.12). After changing a helper or renderer, run:

```bash
python3 .github/release-tools/render.py
python3 .github/release-tools/render.py --check
python3 .github/release-tools/test_release.py
actionlint -shellcheck='' .github/workflows/go-build.yml .github/workflows/go-release.yml \
  .github/workflows/npm-build.yml .github/workflows/npm-release.yml \
  .github/workflows/rust-build.yml .github/workflows/rust-release.yml
```

These local checks validate rendering, focused helper behavior, and GitHub Actions syntax. They do not publish a package or prove registry-side trusted-publisher configuration. Verify OIDC configuration in a disposable or planned first release before removing any legacy manual publishing access.

## References

- [npm trusted publishing](https://docs.npmjs.com/trusted-publishers/)
- [crates.io trusted publishing](https://crates.io/docs/trusted-publishing)
- [Cargo publishing](https://doc.rust-lang.org/cargo/reference/publishing.html)
- [Cargo registry publish wire format](https://doc.rust-lang.org/cargo/reference/registry-web-api.html)
- [Go module release versions](https://go.dev/doc/modules/version-numbers)
- [GitHub OIDC claims in reusable workflows](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-with-reusable-workflows)
