# BonfireTrunk/.github

Organization-wide defaults for [BonfireTrunk](https://github.com/BonfireTrunk) repositories. GitHub uses each file here for any repository that has no file of its own.

| Path | Purpose |
| :-- | :-- |
| [`profile/README.md`](profile/README.md) | The organization profile shown at github.com/BonfireTrunk |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | How to contribute |
| [`SECURITY.md`](SECURITY.md) | How to report a vulnerability privately |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | Contributor Covenant 2.1 |
| [`CLA.md`](CLA.md) | BonfireTrunk Contributor Agreement v1, permanently available at tag `cla-v1` |
| [`pull_request_template.md`](pull_request_template.md) | Default pull request description, with the agreement checkbox |
| [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE) | Issue forms for bugs, feature requests and questions |
| [`.github/workflows/cla-check.yml`](.github/workflows/cla-check.yml) | Reusable workflow that checks the agreement box on pull requests |
| [`.github/workflows/cla.yml`](.github/workflows/cla.yml) | Runs the agreement check on this repository's own pull requests |
| [`.github/workflows/maven-build.yml`](.github/workflows/maven-build.yml) | Reusable workflow that builds and tests a Maven library on pull requests, using GitHub Packages for organization artifacts Maven Central lacks |
| [`.github/workflows/maven-release.yml`](.github/workflows/maven-release.yml) | Reusable workflow that publishes a Maven library to GitHub Packages, tags it with a GitHub release, then publishes it to Maven Central, and publishes a snapshot to GitHub Packages when a merge asks for one |

## Using the shared workflows

A Maven library calls the shared workflows from three small files of its own. The check names they produce, `build / build` and `cla / check`, are the ones the library's `prime` ruleset requires.

`.github/workflows/build.yml`:

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
    uses: BonfireTrunk/.github/.github/workflows/maven-build.yml@prime
```

`.github/workflows/release.yml`:

```yaml
name: release

on:
  push:
    branches: [prime]
  workflow_dispatch:

permissions: {}

concurrency:
  group: release
  cancel-in-progress: false

jobs:
  release:
    permissions:
      contents: write
      packages: write
    uses: BonfireTrunk/.github/.github/workflows/maven-release.yml@prime
    secrets:
      CENTRAL_USERNAME: ${{ secrets.CENTRAL_USERNAME }}
      CENTRAL_TOKEN: ${{ secrets.CENTRAL_TOKEN }}
      MAVEN_GPG_KEY: ${{ secrets.MAVEN_GPG_KEY }}
      MAVEN_GPG_PASSPHRASE: ${{ secrets.MAVEN_GPG_PASSPHRASE }}
```

`.github/workflows/cla.yml`:

```yaml
name: cla

on:
  pull_request:
    types: [opened, edited, reopened, synchronize]

permissions: {}

jobs:
  cla:
    uses: BonfireTrunk/.github/.github/workflows/cla-check.yml@prime
```

A release is a pull request titled `Release X.Y.Z` that sets that version in `pom.xml` and adds a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`. Merging it builds and signs the release once, publishes it to GitHub Packages, creates the tag `vX.Y.Z` with its GitHub release, and then uploads it to Maven Central. If the Maven Central step fails, for example because the organization is over Central's monthly publishing limits, re-run the failed job or run the release workflow on the tag later; the run skips whatever is already done. Maven Central is where users get the library; GitHub Packages requires a token even to download public packages. The library also needs the Maven Wrapper (`./mvnw`) and access to the four release secrets, which are organization secrets limited to selected repositories.

Both shared workflows take an optional `maven-args` input, set with `with:` under the calling job. In `maven-build.yml` it replaces `verify`; in `maven-release.yml` it adds arguments to the publishing build. A library whose tests need a service the runner does not have, such as a Redis server, uses it to skip those tests in both.

### GitHub Packages as a fallback

A pull request whose title starts with `Snapshot:` (for example `Snapshot: Add a borrow timeout`) publishes the library's `-SNAPSHOT` version to GitHub Packages when it is merged into `prime`. Other merges publish nothing, so snapshots exist only where a dependent library needs one. Pull request builds look for `today.bonfire.oss` artifacts on Maven Central first and on GitHub Packages second, so a library can build against another library's snapshot, or against a release that Central has not accepted yet, without waiting for a Central release. Nothing else is ever downloaded from GitHub Packages. The workflow token is enough to read the organization's public packages, so a library's `build.yml` needs no extra permission or secret.

A release resolves from Maven Central alone and fails early if it depends on a SNAPSHOT, so a published library never depends on something its users cannot download. Before releasing a library that was built against another library's snapshot, release that library and switch to its release version.

To use snapshots locally, add a `github` server with a personal access token (classic) that has `read:packages`, and the `https://maven.pkg.github.com/BonfireTrunk/*` repository, to `~/.m2/settings.xml`.
