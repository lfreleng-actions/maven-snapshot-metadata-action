# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Entry points: read the action's inputs and run a mode."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from snapshot_metadata import ActionError
from snapshot_metadata.coordinates import (
    VERSION_RE,
    discover_coordinates,
    top_level_group_paths,
)
from snapshot_metadata.nexus import (
    MAX_RETRY_DELAY,
    Fetcher,
    basic_auth,
    repository_base_url,
)
from snapshot_metadata.repository import prune_metadata, seed_metadata
from snapshot_metadata.workflow import (
    emit,
    emit_error,
    mask,
    write_outputs,
    write_summary,
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"INPUT_{name}", default).strip() or default


def _int_input(name: str, value: str, low: int, high: int) -> int:
    if not re.fullmatch(r"[0-9]{1,3}", value) or not low <= int(value) <= high:
        raise ActionError(f"{name} must be an integer from {low} to {high}")
    return int(value)


def _within(path: Path, root: Path, name: str) -> Path:
    resolved = path.resolve()
    if resolved != root.resolve() and root.resolve() not in resolved.parents:
        raise ActionError(f"{name} must resolve within {root}")
    return resolved


def _workspace() -> Path:
    return Path(os.environ.get("GITHUB_WORKSPACE") or os.getcwd())


def default_baseline() -> Path:
    """Where fetch keeps the baseline: under RUNNER_TEMP, off the workspace."""
    runner_temp = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(runner_temp) / "maven-snapshot-metadata" / "baseline"


def _m2repo() -> Path:
    workspace = _workspace()
    raw = Path(_env("M2REPO_PATH") or str(workspace / "m2repo"))
    return _within(
        raw if raw.is_absolute() else workspace / raw, workspace, "m2repo_path"
    )


def _fetcher() -> Fetcher:
    password = os.environ.get("INPUT_NEXUS_PASSWORD", "")
    mask(password)
    return Fetcher(
        basic_auth(_env("NEXUS_USERNAME"), password),
        _int_input("fetch_attempts", _env("FETCH_ATTEMPTS", "4"), 1, 10),
        _int_input("retry_delay", _env("RETRY_DELAY", "2"), 0, MAX_RETRY_DELAY),
    )


def run_fetch() -> None:
    """Entry point for mode 'fetch'."""
    fetcher = _fetcher()
    base_url = repository_base_url(
        _env("NEXUS_SERVER"), _env("REPOSITORY_NAME"), _env("NEXUS_VERSION", "2")
    )
    workspace = _workspace()
    project_dir = _within(
        workspace / _env("PATH_PREFIX", "."), workspace, "path_prefix"
    )
    pom_file = _env("POM_FILE", "pom.xml")
    pom = _within(project_dir / pom_file, workspace, "pom_file")
    # resolve() is not strict, so a path that does not exist passes the
    # checks above; say which input is wrong before Maven is involved
    if not project_dir.is_dir():
        raise ActionError(f"path_prefix {project_dir} is not a directory")
    if not pom.is_file():
        raise ActionError(f"pom_file {pom} does not exist")
    help_version = _env("HELP_PLUGIN_VERSION", "3.5.2")
    if not VERSION_RE.fullmatch(help_version):
        raise ActionError("help_plugin_version is not a valid version")

    with tempfile.TemporaryDirectory() as work:
        coordinates = discover_coordinates(
            project_dir, pom_file, _env("MAVEN_ARGS"), help_version, Path(work)
        )
    baseline = default_baseline()
    result = seed_metadata(coordinates, base_url, fetcher, _m2repo(), baseline)
    groups = top_level_group_paths(coordinates)
    snapshots = sum(1 for c in coordinates if c.is_snapshot)

    emit(f"Reactor modules: {len(coordinates)} ({snapshots} SNAPSHOT)")
    emit(f"Metadata seeded: {len(result.metadata)} ({result.files} files)")
    for relative in result.metadata:
        emit(f"  {relative}")
    write_outputs(
        {
            "module_count": str(len(coordinates)),
            "metadata_count": str(len(result.metadata)),
            "group_paths": " ".join(groups),
            "baseline_path": str(baseline),
        }
    )
    write_summary(
        [
            "### 📦 SNAPSHOT metadata: fetch",
            "",
            "| Item | Value |",
            "| ---- | ----- |",
            f"| Reactor modules | {len(coordinates)} ({snapshots} SNAPSHOT) |",
            f"| Metadata files seeded | {len(result.metadata)} |",
            f"| Requests made | {fetcher.requests} |",
            f"| Group paths | {' '.join(f'`{g}`' for g in groups)} |",
        ]
    )


def run_prune() -> None:
    """Entry point for mode 'prune'."""
    raw = _env("BASELINE_PATH")
    result = prune_metadata(_m2repo(), Path(raw) if raw else default_baseline())
    emit(f"Metadata pruned: {len(result.removed)} ({result.removed_files} files)")
    for relative in result.removed:
        emit(f"  {relative}")
    emit(f"Metadata left to publish: {result.kept}")
    write_outputs(
        {"removed_count": str(len(result.removed)), "kept_count": str(result.kept)}
    )
    write_summary(
        [
            "### 📦 SNAPSHOT metadata: prune",
            "",
            "| Item | Value |",
            "| ---- | ----- |",
            f"| Unchanged metadata removed | {len(result.removed)} |",
            f"| Metadata left to publish | {result.kept} |",
        ]
    )


def main() -> int:
    """Dispatch on the action's mode input."""
    modes = {"fetch": run_fetch, "prune": run_prune}
    mode = _env("MODE")
    try:
        if mode not in modes:
            raise ActionError("mode must be 'fetch' or 'prune'")
        modes[mode]()
    except ActionError as exc:
        emit_error(str(exc))
        return 1
    except OSError as exc:
        # Filesystem and process failures (a vanished directory, a
        # permission error, Maven not starting) are the caller's
        # environment, not a bug, so they get an annotation too. Other
        # exceptions are bugs, and keep their traceback.
        emit_error(f"{type(exc).__name__}: {exc}")
        return 1
    return 0
