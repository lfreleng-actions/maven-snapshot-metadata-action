# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The maven_args allow-list, applied to maven_args and MAVEN_ARGS."""

from __future__ import annotations

from collections.abc import Iterator

from snapshot_metadata import ActionError

QUIET_FLAGS = frozenset({"-q", "--quiet"})

# maven_args is checked against an allow-list, not a deny-list. Maven's
# parser accepts more spellings than a deny-list can anticipate: it
# bursts short-option clusters (-qN is -q -N on Maven 4), takes long
# options after one hyphen (-non-recursive, on Maven 3 and 4) and
# abbreviated (--non-r, on Maven 4). Each of those narrowed a real
# reactor to one module. So a token passes only in a form listed
# here; anything else, including every cluster, is refused.
#
# The options come from Maven's own CLI definitions, keeping those that
# shape how the reactor resolves. Left out deliberately: project
# selection and reactor narrowing (-f, -pl, -N, -r, -rf, -am, -amd),
# -af, which reads further arguments from a file, -l, which would send
# the project listing fetch reads to a file, and the modes that do
# something other than build (--shell, --up, --enc, --help, --version
# and the like).

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
        + " reactor resolves, each spelled out on its own: no"
        + " clusters such as -qB, no abbreviations, no goals or phases"
    )


def _walk(args: list[str]) -> Iterator[tuple[str, bool]]:
    """Yield each token, and whether it is an option's value.

    Raises for a token outside the allow-list or a missing value.
    """
    pending = optional = False
    for arg in args:
        if pending:
            pending = False
            yield arg, True
            continue
        if optional:
            optional = False
            if not arg.startswith("-"):
                yield arg, True
                continue
        if not arg.startswith("-") or arg == "-":
            raise ActionError(
                f"maven_args may carry options, not goals or phases ({arg!r});"
                + " fetch runs help:active-profiles and nothing else"
            )
        kind, attached = _classify(arg)
        pending = kind == "value" and not attached
        optional = kind == "optional"
        yield arg, False
    if pending:
        raise ActionError(f"maven_args ends with {args[-1]!r}, missing its value")


def split_maven_args(maven_args: str) -> list[str]:
    """Split caller arguments on whitespace, accepting safe options alone.

    fetch must read the whole reactor, or an omitted module deploys from
    build 1, so anything outside the allow-list is refused, including
    goals and phases, which would run before ``help:active-profiles``.
    -q and --quiet pass the check but are dropped: they shape no
    reactor, and would hide the INFO output fetch reads.
    """
    return [
        arg
        for arg, is_value in _walk(maven_args.split())
        if is_value or arg not in QUIET_FLAGS
    ]
