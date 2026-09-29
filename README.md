<!--
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
-->

# 📦 Maven SNAPSHOT Metadata

<!-- prettier-ignore-start -->
<!-- markdownlint-disable-next-line MD013 -->
[![Linux Foundation](https://img.shields.io/badge/Linux-Foundation-blue)](https://linuxfoundation.org/) [![Source Code](https://img.shields.io/badge/GitHub-100000?logo=github&logoColor=white&color=blue)](https://github.com/lfreleng-actions/maven-snapshot-metadata-action) [![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![pre-commit.ci status badge]][pre-commit.ci results page] [![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/lfreleng-actions/maven-snapshot-metadata-action/badge)](https://scorecard.dev/viewer/?uri=github.com/lfreleng-actions/maven-snapshot-metadata-action)
<!-- prettier-ignore-end -->

Seeds and prunes Maven SNAPSHOT metadata around a local-repository
deploy, so SNAPSHOT `buildNumber`s on Nexus keep counting up.

## maven-snapshot-metadata-action

A Maven merge lane builds once, deploys the SNAPSHOTs to a local file
repository (the `m2repo`), and publishes that tree to Nexus from a
separate job. Without help, that fails in two ways:

- **Numbering restarts.** `maven-deploy-plugin` numbers a SNAPSHOT from
  the `maven-metadata.xml` it finds in the target repository. An empty
  `m2repo` starts every deploy at build 1, so each publish reuses
  numbers already on Nexus.
- **Publishing reverts siblings.** Seeded metadata for a module the
  build did not deploy is stale by publish time if another build
  published that module meanwhile. Uploading it would roll Nexus back.

This action handles both, in two modes that bracket the build:

| Mode    | Runs          | Does                                             |
| ------- | ------------- | ------------------------------------------------ |
| `fetch` | Before deploy | Seeds published metadata; keeps a baseline copy  |
| `prune` | After deploy  | Deletes metadata the deploy left unchanged       |

global-jjb has always paired `maven-fetch-metadata.sh` with its deploy
for the same reason; this is that behaviour as a reusable, tested
action.

## Usage Example

<!-- markdownlint-disable MD046 -->

```yaml
steps:
  - uses: actions/checkout@<sha>
  - uses: actions/setup-java@<sha>
    with:
      distribution: temurin
      java-version: '21'

  - name: "Fetch published SNAPSHOT metadata"
    id: fetch
    uses: lfreleng-actions/maven-snapshot-metadata-action@<sha>
    with:
      mode: fetch
      nexus_server: 'https://nexus.example.org'
      repository_name: 'snapshots'

  - name: "Build and deploy to the m2repo"
    id: build
    uses: lfreleng-actions/maven-build-action@<sha>
    with:
      mvn-phases: 'clean deploy'

  - name: "Prune unchanged metadata"
    uses: lfreleng-actions/maven-snapshot-metadata-action@<sha>
    with:
      mode: prune
      m2repo_path: ${{ steps.build.outputs.m2repo_path }}
```

<!-- markdownlint-enable MD046 -->

Upload the pruned `m2repo` as an artefact, and publish it from a job
that holds the Nexus write credentials; this job needs read access at
most.

### Run one lane at a time per repository

`prune` protects metadata this build left alone, and no further. Every
module it deploys rewrites two files: the version-level metadata,
holding the `buildNumber`, and the artifact-level metadata, which lists
**every** version of the artifact. Two lanes running at once go wrong
in both:

- **Same version:** both fetch build N, both deploy N+1, and the later
  publish overwrites the earlier one.
- **Different versions**, such as `master` at `1.1.0-SNAPSHOT` and a
  stable branch at `1.0.1-SNAPSHOT`: both fetch the same artifact-level
  file, each adds its own version, and the later publish drops the
  other's entry from the list.

Nexus offers nothing to lock against, so serialise every lane that
publishes to the repository, whatever its branch, from fetch through
publish. Never cancel a running one, or it may stop between fetch and
publish:

<!-- markdownlint-disable MD046 -->

```yaml
concurrency:
  group: maven-merge-${{ github.repository }}
  cancel-in-progress: false
```

<!-- markdownlint-enable MD046 -->

Keep the key free of the branch name: a per-branch key is what lets the
second case happen.

A concurrency group covers one GitHub repository and no more. It cannot
serialise publishers in **different** repositories that touch the same
metadata: two projects deploying under one groupId, or `maven-plugin`
modules, which share their group's plugin index. Nothing in GitHub or
Nexus locks across repositories, so keep each repository's metadata
paths disjoint, giving every project its own groupId, or coordinate
those publishers outside GitHub. `fetch` reports the groups a reactor
deploys to as `group_paths`, which makes an overlap easy to spot.

## How fetch finds the modules

`fetch` runs `maven-help-plugin`'s `effective-pom` goal once over the
whole reactor. Maven itself applies parent inheritance and property
interpolation, so a module that inherits its `groupId` or `version`
resolves as the deploy will see it. A version still holding a
`${...}` property fails with a hint to define it in `maven_args`.

For each module it requests what the deploy plugin reads, and no more:

- `<group>/<artifact>/maven-metadata.xml`, the version list;
- `<group>/<artifact>/<version>/maven-metadata.xml`, holding the
  `buildNumber`, for SNAPSHOT versions;
- `<group>/maven-metadata.xml` for `maven-plugin` packaging;
- the `.md5` and `.sha1` checksums of each, when published.

It never crawls the group tree, so the request count scales with the
module count rather than the repository's history.

> ⚠️ **fetch runs Maven over the checkout.** Maven loads project
> extensions while building the effective POM, so treat the checkout as
> executable input, as the build that follows does. Run this
> action where the build runs, on merged code.

## Failure handling

`fetch` fails closed. A 404 means "not published yet", and fetch moves
on. Anything else stops the job rather than seeding less than the
server holds, which would restart numbering without warning:

| Response                           | Handling                          |
| ---------------------------------- | --------------------------------- |
| 404                                | Not published yet; skipped        |
| 401, 403                           | Fails at once; check credentials  |
| 3xx redirect                       | Fails; never followed (see below) |
| 429, 5xx, network or timeout       | Retried with doubling delays      |
| Other 4xx                          | Fails at once                     |
| A body without a `<metadata>` root | Fails, e.g. an XML error page     |

The action refuses redirects rather than following them, because
following one would send the `Authorization` header to wherever it
points. Set
`nexus_server` to the canonical `https` URL. For the same reason the
action connects directly and ignores `HTTPS_PROXY`.

## Inputs

<!-- markdownlint-disable MD013 -->

| Name                | Required | Default                    | Description                                             |
| ------------------- | -------- | -------------------------- | ------------------------------------------------------- |
| mode                | True     |                            | `fetch` before the build, `prune` after it              |
| nexus_server        | fetch    |                            | Nexus server URL; `https`, or `http` for loopback       |
| repository_name     | fetch    |                            | Nexus repository the SNAPSHOTs publish to               |
| nexus_version       | False    | `2`                        | Repository URL layout: `2` or `3`                       |
| nexus_username      | False    |                            | Username for a repository that requires read access     |
| nexus_password      | False    |                            | Password for `nexus_username`; the two come as a pair   |
| path_prefix         | False    | `.`                        | Project directory, relative to the workspace            |
| pom_file            | False    | `pom.xml`                  | POM to read the reactor from, relative to `path_prefix` |
| maven_args          | False    |                            | Extra Maven arguments for reading the reactor           |
| help_plugin_version | False    | `3.5.2`                    | `maven-help-plugin` version that reads the reactor      |
| fetch_attempts      | False    | `4`                        | Attempts per file on transient failures, 1 to 10        |
| retry_delay         | False    | `2`                        | Seconds before the first retry, doubling, 0 to 60       |
| m2repo_path         | False    | `$GITHUB_WORKSPACE/m2repo` | Local deploy repository, within the workspace           |
| baseline_path       | False    | fetch's location           | Baseline written by `fetch`, if it moved                |

<!-- markdownlint-enable MD013 -->

The `m2repo_path` default matches the fixed path
[maven-build-action](https://github.com/lfreleng-actions/maven-build-action)
deploys to. Pass its `m2repo_path` output rather than assuming it.

`maven_args` splits on whitespace without a shell, and accepts the
options that shape how the effective POM resolves, and nothing else:

<!-- markdownlint-disable MD013 -->

| Kind       | Options                                                                                                 |
| ---------- | ------------------------------------------------------------------------------------------------------- |
| Settings   | `-s`, `-gs`, `-is`, `-ps`, `-t`, `-gt`, `-it`                                                           |
| Resolution | `-P`, `-D`, `-o`, `-U`, `-nsu`, `-itr`, `-canf`, `-sadp`, and `-C` or `-c` for strict or lax checksums  |
| Behaviour  | `-B`, `-q`, `-e`, `-X`, `-V`, `-ntp`, `-T`, `-b`, `-fae`, `-ff`, `-fn`, `--color`, `--fail-on-severity` |

<!-- markdownlint-enable MD013 -->

Pass each by its short or long name, with a value attached (`-Pci`,
`--settings=s.xml`) or as the next argument. `fetch` refuses anything
else, since a module it misses would deploy from build 1:

- goals and phases, such as `deploy`, which would run before
  `help:effective-pom` and publish before `fetch` seeds any metadata;
- project selection and reactor narrowing: `-f`, `-pl`, `-N`, `-r`,
  `-rf`, `-am`, `-amd`;
- `-af`, which reads further, unchecked arguments from a file;
- any other spelling Maven's parser would also accept: clusters such as
  `-qN`, long names after one hyphen such as `-non-recursive`, and
  abbreviations such as `--non-r`.

That list is an allow-list because Maven accepts more spellings than a
deny-list can foresee. In real runs, `-qN` and `--non-r` narrowed the
effective POM to one module on Maven 4, and `-non-recursive` did on
Maven 3 and Maven 4 alike.

`fetch` checks a workflow-level `MAVEN_ARGS` against the same list and
replays it ahead of `maven_args`, where Maven itself would put it. The
deploy that follows honours `MAVEN_ARGS`, so `fetch` must see the
reactor it selects: a `-P` there that adds modules would otherwise leave
them without seeded metadata. Anything the list refuses, such as `-pl`,
fails the step, and Maven never reads the variable unchecked from the
environment. A project's own `.mvn/maven.config` still applies: as part
of the checkout, the deploy reads it too, so both see the same reactor.

The Nexus input names match
[nexus-publish-action](https://github.com/lfreleng-actions/nexus-publish-action),
which lets a lane pass the same values to both.

## Outputs

<!-- markdownlint-disable MD013 -->

| Name           | Mode  | Description                                                   |
| -------------- | ----- | ------------------------------------------------------------- |
| module_count   | fetch | Reactor modules read                                          |
| metadata_count | fetch | Metadata files seeded, excluding checksums                    |
| group_paths    | fetch | Space-separated top-level group paths, e.g. `org/example`     |
| baseline_path  | fetch | Where fetch keeps the pristine metadata                       |
| removed_count  | prune | Unchanged metadata files removed                              |
| kept_count     | prune | Metadata files left in the m2repo to publish                  |

<!-- markdownlint-enable MD013 -->

`group_paths` lists every top-level group the reactor deploys to, so a
publish step can cover artefacts outside the root `groupId` too.

## Requirements

- Python 3.9 or newer as `python3`; GitHub-hosted runners ship 3.12.
  The action needs nothing beyond the standard library, and checks the
  version before it starts.
- For `fetch`, `mvn` on `PATH` and a JDK the project builds with.
  GitHub-hosted runners ship Maven; `actions/setup-java` provides the
  JDK.
- Egress to the Nexus server, and to whatever repositories Maven needs
  to resolve the project's parent POMs.

The baseline lives under `$RUNNER_TEMP`, outside the workspace, so
workspace cleanup, `git clean` and artefact uploads of the workspace
leave it alone. That separation is not a security boundary, though:
build code runs as the same user and can reach `$RUNNER_TEMP` too. Like
`fetch` itself, `prune` trusts the build it brackets, which is why both
belong on merged code. `fetch` marks the baseline complete as its last
act, so `prune` refuses one that an interrupted fetch left behind.

Maven runs without the `INPUT_*` variables this action sets for its own
inputs. They include `nexus_password`, which Maven doesn't need and
project extensions could otherwise read. A job's own `INPUT_*` variables
still reach Maven, since a profile may activate on one and the deploy
that follows sees it too.

`fetch` requires an `m2repo` with no `maven-metadata.xml` in it yet.
Leftover metadata the server no longer holds would stay out of the
baseline, so `prune` would never judge it and it would publish
unchanged. Artefacts already there are fine. `fetch` refuses rather
than deletes, since it cannot tell a leftover from a file you meant.

## Implementation Details

The logic lives in the `src/snapshot_metadata` package, where the
linters and `tests/test_snapshot_metadata.py` reach it. The suite runs against
`tests/mock_nexus.py`, a mock Nexus serving a repository tree over a
loopback port, and covers first publish, partial history,
authentication failure, retries, redirects and a sibling published
during the build.

The workflow in `.github/workflows/testing.yaml` also runs the whole
sequence on a real Maven deploy. It seeds `buildNumber` 41, deploys one
module, then asserts the deploy continued at 42 and that `prune` dropped
the untouched module's metadata. It runs on Maven 3.9, the runner's own,
and on Maven 4.

[pre-commit.ci results page]: https://results.pre-commit.ci/latest/github/lfreleng-actions/maven-snapshot-metadata-action/main
[pre-commit.ci status badge]: https://results.pre-commit.ci/badge/github/lfreleng-actions/maven-snapshot-metadata-action/main.svg
