# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Reactor coordinates, as Maven lists the projects it builds."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from snapshot_metadata import ActionError
from snapshot_metadata.maven_args import split_maven_args
from snapshot_metadata.workflow import emit_untrusted

METADATA = "maven-metadata.xml"
DEFAULT_POM = "pom.xml"
HELP_PLUGIN = "org.apache.maven.plugins:maven-help-plugin"
DISCOVERY_TIMEOUT = 15 * 60
MAVEN_VERSION_TIMEOUT = 2 * 60
MINIMUM_MAVEN = (3, 9)
MAVEN_VERSION_RE = re.compile(r"^Apache Maven ([0-9]+)\.([0-9]+)", re.M)
# Opens each project's entry in help:active-profiles output: g:a:p:v
PROJECT_LINE_RE = re.compile(r"^Active Profiles for Project '([^']*)':$")
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Maven coordinate segments. Anything outside these sets would have to
# be escaped to form a repository path, and would be a strong sign the
# listing is not what Maven meant it to be.
GROUP_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")


@dataclass(frozen=True, order=True)
class Coordinate:
    """One reactor module, as Maven lists it."""

    group_id: str
    artifact_id: str
    version: str
    packaging: str

    @property
    def group_path(self) -> str:
        """Repository path of the groupId, e.g. ``org/example``."""
        return self.group_id.replace(".", "/")

    @property
    def is_snapshot(self) -> bool:
        """Whether the version is a SNAPSHOT."""
        return self.version.endswith("-SNAPSHOT")

    def metadata_paths(self) -> list[str]:
        """Metadata files ``maven-deploy-plugin`` reads for this module.

        The artifact-level file lists the module's versions. A SNAPSHOT
        also has a version-level file carrying the ``buildNumber`` the
        next deploy continues from. A ``maven-plugin`` also updates the
        group-level plugin index.
        """
        artifact = f"{self.group_path}/{self.artifact_id}"
        paths = [f"{artifact}/{METADATA}"]
        if self.is_snapshot:
            paths.append(f"{artifact}/{self.version}/{METADATA}")
        if self.packaging == "maven-plugin":
            paths.append(f"{self.group_path}/{METADATA}")
        return paths

    def record(self) -> dict[str, str]:
        """This module as fetch records it, under Maven's field names."""
        return {
            "groupId": self.group_id,
            "artifactId": self.artifact_id,
            "version": self.version,
            "packaging": self.packaging,
        }


# The keys of one module in fetch's coordinates record, in field order
RECORD_FIELDS = ("groupId", "artifactId", "version", "packaging")


def from_record(entry: object) -> Coordinate:
    """Read one module back from fetch's record, checked as on discovery.

    The record sits beside the baseline, where build code can reach it,
    so a value that would not have passed discovery is refused here too.
    """
    if not isinstance(entry, dict):
        raise ActionError("an entry is not an object")
    values = cast("dict[object, object]", entry)
    if sorted(values, key=str) != sorted(RECORD_FIELDS):
        raise ActionError(f"an entry's keys are not {', '.join(RECORD_FIELDS)}")
    fields = [values[name] for name in RECORD_FIELDS]
    strings = [value for value in fields if isinstance(value, str)]
    if len(strings) != len(fields):
        raise ActionError("an entry holds a value that is not a string")
    group_id, artifact_id, version, packaging = strings
    coordinate = Coordinate(group_id, artifact_id, version, packaging)
    _validate(coordinate)
    return coordinate


def _validate(coordinate: Coordinate) -> None:
    fields = {
        "groupId": (coordinate.group_id, GROUP_RE),
        "artifactId": (coordinate.artifact_id, TOKEN_RE),
        "version": (coordinate.version, VERSION_RE),
        "packaging": (coordinate.packaging, TOKEN_RE),
    }
    for field, (value, pattern) in fields.items():
        if "${" in value:
            raise ActionError(
                f"{field} '{value}' holds an unresolved property; pass its"
                + " value through maven_args, e.g. -Drevision=1.0.0-SNAPSHOT"
            )
        if not pattern.fullmatch(value) or value in {".", ".."}:
            raise ActionError(f"{field} {value!r} is not a valid coordinate")


def parse_active_profiles(text: str) -> list[Coordinate]:
    """Read every module's coordinates from ``help:active-profiles`` output.

    Maven lists each reactor project by its effective model's id, with
    inheritance and interpolation applied. The list is one INFO message,
    so it is complete or absent, and absent fails.
    """
    coordinates: set[Coordinate] = set()
    for line in ANSI_ESCAPE_RE.sub("", text).splitlines():
        found = PROJECT_LINE_RE.match(line.strip())
        if found is None:
            continue
        fields = found.group(1).split(":")
        if len(fields) != 4:
            raise ActionError(f"unexpected project id {found.group(1)!r}")
        group_id, artifact_id, packaging, version = fields
        coordinate = Coordinate(group_id, artifact_id, version, packaging)
        _validate(coordinate)
        coordinates.add(coordinate)
    if not coordinates:
        raise ActionError(
            "help:active-profiles listed no projects. Maven prints them at"
            + " INFO level, so a -q in .mvn/maven.config hides them, and an"
            + " 'output' property from any source sends them to a file"
        )
    return sorted(coordinates)


# The environment variables action.yaml sets for this step: one per
# declared input, under a prefix of the action's own. Only these leave
# Maven's environment. A caller's job variables stay, INPUT_* ones
# included, since a profile may activate on one and the deploy that
# follows would still see it. A test keeps this list in step with
# action.yaml.
ACTION_INPUT_PREFIX = "SNAPSHOT_METADATA_"
ACTION_INPUT_VARIABLES = frozenset(
    ACTION_INPUT_PREFIX + name
    for name in (
        "BASELINE_PATH", "CHECK_COORDINATES", "FETCH_ATTEMPTS",
        "HELP_PLUGIN_VERSION", "M2REPO_PATH", "MAVEN_ARGS", "MODE",
        "NEXUS_PASSWORD", "NEXUS_SERVER", "NEXUS_USERNAME", "NEXUS_VERSION",
        "PATH_PREFIX", "POM_FILE", "REPOSITORY_NAME", "RETRY_DELAY",
    )
)  # fmt: skip


def maven_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment for Maven: the caller's, minus this action's inputs.

    The inputs include the Nexus password, which Maven does not need.
    Project extensions and plugins run inside Maven and can read its
    environment, so the credential must not reach them.

    ``MAVEN_ARGS`` leaves the environment as well: Maven 3.9 and later
    prepend it to the command line unchecked. discover_coordinates
    validates it and replays it explicitly instead, since the deploy
    that follows honours it and fetch must see the same reactor.
    """
    return {
        k: v
        for k, v in environ.items()
        if k not in ACTION_INPUT_VARIABLES and k != "MAVEN_ARGS"
    }


def require_maven_version(mvn: str, work_dir: Path) -> None:
    """Refuse a Maven older than MINIMUM_MAVEN, the floor fetch mirrors.

    Before 3.9 the launcher ignores ``MAVEN_ARGS`` (MNG-7193), so the
    deploy would not see what fetch replays from it, and could deploy
    modules or versions fetch never seeded. The maven_args allow-list
    also follows the option parsers of 3.9 and 4, not older ones. Run
    from work_dir, where no project's .mvn configuration applies.
    """
    try:
        result = subprocess.run(
            [mvn, "-B", "--version"],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=MAVEN_VERSION_TIMEOUT,
            env=maven_environment(os.environ),
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError("mvn --version did not finish in 2 minutes") from exc
    found = MAVEN_VERSION_RE.search(result.stdout)
    if result.returncode != 0 or found is None:
        emit_untrusted(result.stdout + result.stderr)
        raise ActionError(
            f"mvn --version reported no Maven version (exit {result.returncode})"
        )
    major, minor = int(found.group(1)), int(found.group(2))
    if (major, minor) < MINIMUM_MAVEN:
        raise ActionError(f"fetch needs Maven 3.9 or newer; mvn is {major}.{minor}")


def _launcher_safe(value: str) -> str:
    """Refuse MAVEN_ARGS that Maven's launcher would read differently.

    The launcher expands it unquoted, so the shell splits it on space,
    tab and newline and expands globs against the working directory;
    a literal replay of such a value can name other properties or
    profiles than the deploy sees.
    """
    if re.search(r"[*?\[]", value):
        raise ActionError("holds a glob character the launcher would expand")
    if any(c.isspace() and c not in " \t\n" for c in value):
        raise ActionError("holds whitespace the launcher would not split on")
    return value


def discover_coordinates(
    project_dir: Path,
    pom_file: str,
    maven_args: str,
    help_plugin_version: str,
    work_dir: Path,
    mvn: str = "mvn",
) -> list[Coordinate]:
    """List the reactor with ``help:active-profiles`` and read it.

    Not ``help:effective-pom``: its ``artifact`` parameter, from any
    property source, swaps the reactor for one artifact, and writing it
    to a file defines an ``output`` property the deploy never sees.
    An empty ``pom_file`` reads DEFAULT_POM, checked against a .mvn
    configuration that may select another.
    """
    if shutil.which(mvn) is None:
        raise ActionError(f"'{mvn}' not found on PATH; set up Maven first")
    require_maven_version(mvn, work_dir)
    # The deploy will honour a workflow-level MAVEN_ARGS, so fetch must
    # too, or modules or versions it selects would get no seeded
    # metadata. Checked like maven_args and replayed in Maven's own
    # position, ahead of the command line, rather than left for Maven
    # to read unchecked from the environment.
    try:
        ambient = split_maven_args(_launcher_safe(os.environ.get("MAVEN_ARGS", "")))
    except ActionError as exc:
        raise ActionError(f"MAVEN_ARGS: {exc}") from exc
    options = [mvn, "-B", "--no-transfer-progress", *ambient]
    options += split_maven_args(maven_args)
    goal = f"{HELP_PLUGIN}:{help_plugin_version}:active-profiles"
    listed = _list_reactor([*options, "-f", pom_file or DEFAULT_POM, goal], project_dir)
    config = None if pom_file else _maven_config(project_dir)
    if config is None:
        return listed
    # A -f in the config picks the POM a plain 'mvn deploy' builds, but
    # a command-line -f, fetch's or the deploy's own, overrides it. Which
    # one the deploy reads is unknown, so both must give one reactor.
    advice = (
        "; set pom_file to the POM the deploy builds: the one its -f names"
        + " (maven-build-action always passes one), or else the one the"
        + " config selects"
    )
    try:
        plain = _list_reactor([*options, goal], project_dir)
    except ActionError as exc:
        raise ActionError(
            f"with {config} present, help:active-profiles without -f failed{advice}"
        ) from exc
    if plain != listed:
        differ = sorted({c.artifact_id for c in set(plain) ^ set(listed)})
        raise ActionError(
            f"{config} makes a plain mvn read another reactor than"
            + f" {DEFAULT_POM}, differing in {', '.join(differ[:5])}{advice}"
        )
    return listed


def _maven_config(project_dir: Path) -> Path | None:
    """The nearest .mvn/maven.config at or above ``project_dir``, if any.

    Maven reads the config of the first directory up holding .mvn, if it
    has one; taking the nearest config anywhere up errs toward checking.
    """
    for directory in (project_dir, *project_dir.parents):
        config = directory / ".mvn" / "maven.config"
        if config.is_file():
            return config
    return None


def _list_reactor(command: list[str], project_dir: Path) -> list[Coordinate]:
    """Run one help:active-profiles command and read the reactor it lists."""
    try:
        result = subprocess.run(
            command,
            cwd=project_dir,
            capture_output=True,
            # Coordinates are ASCII; any other byte in a log line is noise
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=DISCOVERY_TIMEOUT,
            env=maven_environment(os.environ),
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError("help:active-profiles did not finish in 15 minutes") from exc
    if result.returncode != 0:
        emit_untrusted(result.stdout + result.stderr)
        raise ActionError(f"help:active-profiles failed (exit {result.returncode})")
    try:
        return parse_active_profiles(result.stdout)
    except ActionError:
        emit_untrusted(result.stdout + result.stderr)
        raise


def top_level_group_paths(coordinates: Iterable[Coordinate]) -> list[str]:
    """Distinct group paths, dropping any nested inside another."""
    paths = sorted({c.group_path for c in coordinates})
    return [p for p in paths if not any(p.startswith(f"{q}/") for q in paths)]
