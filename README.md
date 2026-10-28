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
| [`.github/workflows/maven-build.yml`](.github/workflows/maven-build.yml) | Reusable workflow that builds and tests a Maven library on pull requests |
| [`.github/workflows/maven-release.yml`](.github/workflows/maven-release.yml) | Reusable workflow that publishes a Maven library to Maven Central and GitHub Packages and creates its GitHub release |

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

A release is a pull request titled `Release X.Y.Z` that sets that version in `pom.xml` and adds a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`. Merging it publishes the release to Maven Central and to GitHub Packages. Maven Central is where users get the library; GitHub Packages requires a token even to download public packages. The library also needs the Maven Wrapper (`./mvnw`) and access to the four release secrets, which are organization secrets limited to selected repositories.
