# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Reactor coordinates, read from Maven's effective POM."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from snapshot_metadata import ActionError
from snapshot_metadata.workflow import emit_untrusted

METADATA = "maven-metadata.xml"
HELP_PLUGIN = "org.apache.maven.plugins:maven-help-plugin"
EFFECTIVE_POM_TIMEOUT = 15 * 60

# Maven coordinate segments. Anything outside these sets would have to
# be escaped to form a repository path, and would be a strong sign the
# effective POM is not what Maven meant it to be.
GROUP_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[A-Za-z0-9_.+-]+$")
# maven_args is checked against an allow-list, not a deny-list. Maven's
# parser accepts more spellings than a deny-list can anticipate: it
# bursts short-option clusters (-qN is -q -N on Maven 4), takes long
# options after one hyphen (-non-recursive, on Maven 3 and 4) and
# abbreviated (--non-r, on Maven 4). Each of those narrowed a real
# effective POM to one module. So a token passes only in a form listed
# here; anything else, including every cluster, is refused.
#
# The options come from Maven's own CLI definitions, keeping those that
# shape how the effective POM resolves. Left out deliberately: project
# selection and reactor narrowing (-f, -pl, -N, -r, -rf, -am, -amd),
# -af, which reads further arguments from a file, -l, which would hide
# the output fetch reports, and the modes that do something other than
# build (--shell, --up, --enc, --help, --version and the like).

# Flags: short and long name, taking no value.
SAFE_FLAGS = {
    "-B": "--batch-mode", "-U": "--update-snapshots", "-e": "--errors",
    "-X": "--verbose", "-q": "--quiet", "-o": "--offline",
    "-V": "--show-version", "-C": "--strict-checksums",
    "-c": "--lax-checksums", "-fae": "--fail-at-end", "-ff": "--fail-fast",
    "-fn": "--fail-never", "-nsu": "--no-snapshot-updates",
    "-ntp": "--no-transfer-progress",
    "-itr": "--ignore-transitive-repositories",
}  # fmt: skip
# Options taking a value: short name (value attached or next) and long
# name (value after '=' or next). A short name of None is long-only.
SAFE_VALUE_OPTIONS = {
    "-D": "--define", "-P": "--activate-profiles", "-T": "--threads",
    "-s": "--settings", "-gs": "--global-settings", "-t": "--toolchains",
    "-gt": "--global-toolchains", "-is": "--install-settings",
    "-it": "--install-toolchains", "-ps": "--project-settings",
    "-b": "--builder", "-canf": "--cache-artifact-not-found",
    "-sadp": "--strict-artifact-descriptor-policy",
    None: "--fail-on-severity",
}  # fmt: skip
# Takes a value only when one follows: '--color' and '--color never'.
# Maven consumes the next token unless it begins with '-'.
OPTIONAL_VALUE_LONG = {"--color"}
# Why a few options are refused, for the error message. This table
# never decides safety: the allow-list above does.
REFUSAL_REASONS = {
    "-f": "selects a POM; use pom_file",
    "--file": "selects a POM; use pom_file",
    "-pl": "narrows the reactor", "--projects": "narrows the reactor",
    "-N": "narrows the reactor", "--non-recursive": "narrows the reactor",
    "-r": "narrows the reactor", "--resume": "narrows the reactor",
    "-rf": "narrows the reactor", "--resume-from": "narrows the reactor",
    "-am": "narrows the reactor", "--also-make": "narrows the reactor",
    "-amd": "narrows the reactor",
    "--also-make-dependents": "narrows the reactor",
    "-af": "reads unchecked arguments from a file",
    "--at-file": "reads unchecked arguments from a file",
}  # fmt: skip


@dataclass(frozen=True, order=True)
class Coordinate:
    """One reactor module, as the effective POM reports it."""

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


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


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


def parse_effective_pom(path: Path) -> list[Coordinate]:
    """Read every module's coordinates from ``help:effective-pom`` output.

    A reactor writes ``<projects>`` wrapping one ``<project>`` per
    module; a single-module build writes a bare ``<project>``. Maven
    has already applied parent inheritance and property interpolation,
    which is why the action asks it rather than reading the POMs.

    Maven generates the document from a checkout the build is about to
    execute anyway. ElementTree resolves no external entities, and the
    expat it bundles bounds entity expansion.
    """
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ActionError(f"cannot parse the effective POM: {exc}") from exc
    if _local_name(root.tag) == "project":
        projects = [root]
    elif _local_name(root.tag) == "projects":
        projects = [c for c in root if _local_name(c.tag) == "project"]
    else:
        raise ActionError(f"unexpected effective POM root <{root.tag}>")

    coordinates: set[Coordinate] = set()
    for project in projects:
        coordinate = Coordinate(
            group_id=_child_text(project, "groupId"),
            artifact_id=_child_text(project, "artifactId"),
            version=_child_text(project, "version"),
            # Maven's default when a POM declares no packaging
            packaging=_child_text(project, "packaging") or "jar",
        )
        _validate(coordinate)
        coordinates.add(coordinate)
    if not coordinates:
        raise ActionError("the effective POM lists no projects")
    return sorted(coordinates)


def long_option_names() -> set[str]:
    """Every long option Maven defines, allowed, refused or neither.

    The single-hyphen guard needs the complete set: a name missing here
    would read as an attached short value, so '-shell' passed as '-s'
    with 'hell' while Maven versions disagree on what it means. Taken
    from Maven's CommonsCliOptions and CommonsCliMavenOptions.
    """
    return {
        "--activate-profiles", "--also-make", "--also-make-dependents",
        "--at-file", "--batch-mode", "--builder",
        "--cache-artifact-not-found", "--color", "--debug", "--define",
        "--enc", "--errors", "--fail-at-end", "--fail-fast",
        "--fail-never", "--fail-on-severity", "--file",
        "--force-interactive", "--global-settings", "--global-toolchains",
        "--help", "--ignore-transitive-repositories", "--install-settings",
        "--install-toolchains", "--lax-checksums", "--log-file",
        "--no-snapshot-updates", "--no-transfer-progress",
        "--non-interactive", "--non-recursive", "--offline",
        "--project-settings", "--projects", "--quiet", "--raw-streams",
        "--resume", "--resume-from", "--settings", "--shell",
        "--show-version", "--strict-artifact-descriptor-policy",
        "--strict-checksums", "--threads", "--toolchains", "--up",
        "--update-snapshots", "--verbose", "--version", "--yjp",
    }  # fmt: skip


def _classify(arg: str) -> tuple[str, bool]:
    """Classify one token as 'flag', 'value' or 'optional'.

    The bool says whether a value-taking option already carries its
    value (-Dx=y, -sfile, --settings=file). Raises for anything not in
    an allowed form.
    """
    if arg in SAFE_FLAGS or arg in SAFE_FLAGS.values():
        return "flag", False
    if arg in OPTIONAL_VALUE_LONG:
        return "optional", False
    for short, long in SAFE_VALUE_OPTIONS.items():
        if arg == long or arg == short:
            return "value", False
        if arg.startswith(f"{long}="):
            return "value", True
    # Attached short values: longest names first, so -gs is not read
    # as -g with 's...' attached. A token that also spells a long name
    # after one hyphen (-batch-mode) is ambiguous, since Maven accepts
    # single-hyphen long options too, so it is refused, not guessed at.
    single_hyphen_long = {name[1:] for name in long_option_names()}
    if arg.split("=", 1)[0] not in single_hyphen_long:
        for short in sorted(
            (k for k in SAFE_VALUE_OPTIONS if k), key=len, reverse=True
        ):
            if arg.startswith(short) and len(arg) > len(short):
                return "value", True
    if any(arg.startswith(f"{o}=") for o in OPTIONAL_VALUE_LONG):
        return "flag", False
    raise ActionError(_refusal(arg))


def _refusal(arg: str) -> str:
    name = arg.split("=", 1)[0]
    reason = REFUSAL_REASONS.get(name)
    long_reasons = {k: v for k, v in REFUSAL_REASONS.items() if k.startswith("--")}
    if reason is None:
        # A single-hyphen or abbreviated long name: --non-r, -non-recursive
        stem = name.lstrip("-")
        for option, why in long_reasons.items():
            if stem and option[2:].startswith(stem):
                reason = why
                break
    if reason is None and name.startswith("-") and not name.startswith("--"):
        # A cluster such as -qN: name any refused short option inside it
        shorts = {k: v for k, v in REFUSAL_REASONS.items() if not k.startswith("--")}
        for option in sorted(shorts, key=len, reverse=True):
            if option[1:] in name[1:]:
                reason = shorts[option]
                break
    detail = f"; it {reason}" if reason else ""
    return (
        f"maven_args may not pass {arg!r}{detail}. It accepts the settings,"
        + " profile, property and checksum options that shape how the"
        + " effective POM resolves, each spelled out on its own: no"
        + " clusters such as -qB, no abbreviations, no goals or phases"
    )


def split_maven_args(maven_args: str) -> list[str]:
    """Split caller arguments on whitespace, accepting safe options alone.

    fetch must read the whole reactor, or an omitted module deploys from
    build 1, so anything outside the allow-list is refused, including
    goals and phases, which would run before ``help:effective-pom``.
    """
    args = maven_args.split()
    pending = False
    optional = False
    for arg in args:
        if pending:
            pending = False
            continue
        if optional:
            optional = False
            if not arg.startswith("-"):
                continue
        if not arg.startswith("-") or arg == "-":
            raise ActionError(
                f"maven_args may carry options, not goals or phases ({arg!r});"
                + " fetch runs help:effective-pom and nothing else"
            )
        kind, attached = _classify(arg)
        pending = kind == "value" and not attached
        optional = kind == "optional"
    if pending:
        raise ActionError(f"maven_args ends with {args[-1]!r}, missing its value")
    return args


# The environment variables action.yaml sets for this step: one per
# declared input. Only these leave Maven's environment. A caller's own
# INPUT_* job variables stay, since a profile may activate on one and
# the deploy that follows would still see it. A test keeps this list
# in step with action.yaml.
ACTION_INPUT_VARIABLES = frozenset({
    "INPUT_BASELINE_PATH", "INPUT_FETCH_ATTEMPTS",
    "INPUT_HELP_PLUGIN_VERSION", "INPUT_M2REPO_PATH", "INPUT_MAVEN_ARGS",
    "INPUT_MODE", "INPUT_NEXUS_PASSWORD", "INPUT_NEXUS_SERVER",
    "INPUT_NEXUS_USERNAME", "INPUT_NEXUS_VERSION", "INPUT_PATH_PREFIX",
    "INPUT_POM_FILE", "INPUT_REPOSITORY_NAME", "INPUT_RETRY_DELAY",
})  # fmt: skip


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


def discover_coordinates(
    project_dir: Path,
    pom_file: str,
    maven_args: str,
    help_plugin_version: str,
    work_dir: Path,
    mvn: str = "mvn",
) -> list[Coordinate]:
    """Run ``help:effective-pom`` once over the reactor and parse it."""
    if shutil.which(mvn) is None:
        raise ActionError(f"'{mvn}' not found on PATH; set up Maven first")
    # The deploy will honour a workflow-level MAVEN_ARGS, so fetch must
    # too, or modules or versions it selects would get no seeded
    # metadata. Checked like maven_args and replayed in Maven's own
    # position, ahead of the command line, rather than left for Maven
    # to read unchecked from the environment.
    try:
        ambient = split_maven_args(os.environ.get("MAVEN_ARGS", ""))
    except ActionError as exc:
        raise ActionError(f"MAVEN_ARGS: {exc}") from exc
    output = work_dir / "effective-pom.xml"
    command = [
        mvn,
        "-B",
        "-q",
        "--no-transfer-progress",
        *ambient,
        *split_maven_args(maven_args),
        "-f",
        pom_file,
        f"{HELP_PLUGIN}:{help_plugin_version}:effective-pom",
        # After the caller's arguments: Maven takes the last -D given
        f"-Doutput={output}",
    ]
    try:
        result = subprocess.run(
            command,
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=False,
            timeout=EFFECTIVE_POM_TIMEOUT,
            env=maven_environment(os.environ),
        )
    except subprocess.TimeoutExpired as exc:
        raise ActionError("help:effective-pom did not finish in 15 minutes") from exc
    if result.returncode != 0 or not output.is_file():
        emit_untrusted(result.stdout + result.stderr)
        raise ActionError(f"help:effective-pom failed (exit {result.returncode})")
    return parse_effective_pom(output)


def top_level_group_paths(coordinates: Iterable[Coordinate]) -> list[str]:
    """Distinct group paths, dropping any nested inside another."""
    paths = sorted({c.group_path for c in coordinates})
    return [p for p in paths if not any(p.startswith(f"{q}/") for q in paths)]
