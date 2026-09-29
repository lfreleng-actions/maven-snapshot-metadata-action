# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Seeding and pruning metadata in the local deploy repository."""

from __future__ import annotations

import hashlib
import shutil
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from snapshot_metadata import ActionError
from snapshot_metadata.coordinates import METADATA, Coordinate
from snapshot_metadata.nexus import Fetcher

# Seeded alongside each metadata file when the server holds them, with
# the hashlib algorithm each names. These are the checksums Maven's
# resolver writes and verifies by default.
SEEDED_CHECKSUMS = {".md5": "md5", ".sha1": "sha1"}
# Removed alongside a pruned metadata file, whichever exist.
PRUNED_SIBLINGS = (".md5", ".sha1", ".sha256", ".sha512", ".asc")
BASELINE_MARKER = ".maven-snapshot-metadata-baseline"


def looks_like_metadata(body: bytes) -> bool:
    """Whether a response body is Maven repository metadata.

    Parsed rather than prefix-matched: an XML error page or login
    response also starts with a declaration. Only a ``<metadata>``
    root qualifies. The body comes from the configured server, is
    capped at MAX_BODY, and ElementTree resolves no external entities.
    """
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return False
    return root.tag.rsplit("}", 1)[-1] == "metadata"


def checksum_matches(sidecar: bytes, body: bytes, extension: str) -> bool:
    """Whether a checksum file holds the digest of ``body``.

    Recomputed rather than checked for shape: a well-formed but stale
    sidecar, fetched after the metadata changed on the server, would
    otherwise ship and fail Maven's verification. Some tools append a
    filename after the digest, so only the first word counts.
    """
    try:
        words = sidecar.decode("ascii").split()
    except UnicodeDecodeError:
        return False
    digest = hashlib.new(SEEDED_CHECKSUMS[extension], body).hexdigest()
    return bool(words) and words[0].lower() == digest


def safe_target(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing any escape.

    Relative paths come from validated coordinates, so this guards
    against a symlinked directory the build left inside the tree.
    """
    target = root / relative
    resolved_root = root.resolve()
    resolved = target.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ActionError(f"{relative} resolves outside {root}")
    if target.is_symlink():
        raise ActionError(f"{relative} is a symbolic link")
    return target


@dataclass
class FetchResult:
    """What ``fetch`` seeded."""

    metadata: list[str]
    files: int


def _download(fetcher: Fetcher, base_url: str, relative: str) -> dict[str, bytes]:
    """Fetch one metadata file and its checksums; empty when unpublished."""
    body = fetcher.fetch(base_url + relative)
    if body is None:
        return {}
    if not looks_like_metadata(body):
        raise ActionError(f"{relative} on the server is not XML metadata")
    found = {relative: body}
    for extension in SEEDED_CHECKSUMS:
        checksum = fetcher.fetch(base_url + relative + extension)
        if checksum is None:
            continue
        if not checksum_matches(checksum, body, extension):
            raise ActionError(
                f"{relative}{extension} does not match the metadata; the"
                + " server may have changed it mid-fetch, so retry"
            )
        found[relative + extension] = checksum
    return found


def seed_metadata(
    coordinates: Iterable[Coordinate],
    base_url: str,
    fetcher: Fetcher,
    m2repo: Path,
    baseline: Path,
) -> FetchResult:
    """Download published metadata into the m2repo and the baseline.

    The m2repo must hold no metadata beforehand. Leftover metadata the
    server no longer has would stay out of the baseline, so ``prune``
    would never judge it and it would publish unchanged. Refused, not
    deleted: fetch cannot tell a leftover from a file the caller meant.

    The baseline marker goes in last, once every path has seeded, so an
    interrupted fetch leaves a baseline ``prune`` refuses.
    """
    if m2repo.is_dir():
        existing = sorted(
            p.relative_to(m2repo).as_posix()
            for p in m2repo.rglob(f"{METADATA}*")
            if p.is_file()
        )
        if existing:
            raise ActionError(
                f"{m2repo} already holds {len(existing)} metadata file(s),"
                + f" e.g. {existing[0]}; fetch needs a clean m2repo"
            )
    if baseline.exists():
        shutil.rmtree(baseline)
    baseline.mkdir(parents=True)
    m2repo.mkdir(parents=True, exist_ok=True)

    paths = sorted({p for c in coordinates for p in c.metadata_paths()})
    seeded: list[str] = []
    files = 0
    for relative in paths:
        found = _download(fetcher, base_url, relative)
        for path, content in found.items():
            for root in (m2repo, baseline):
                target = safe_target(root, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                _ = target.write_bytes(content)
            files += 1
        if found:
            seeded.append(relative)
    _ = (baseline / BASELINE_MARKER).write_text("fetch\n", encoding="utf-8")
    return FetchResult(metadata=seeded, files=files)


def _verify_siblings(m2repo: Path, relative: str, body: bytes) -> None:
    """Fail when a kept metadata file's checksum does not describe it.

    Maven regenerates checksums as it redeploys, but a seeded sidecar
    the deploy did not rewrite would still hold the old digest.
    """
    for extension in SEEDED_CHECKSUMS:
        sibling = safe_target(m2repo, relative + extension)
        if sibling.is_file() and not checksum_matches(
            sibling.read_bytes(), body, extension
        ):
            raise ActionError(
                f"{relative}{extension} does not match the redeployed"
                + " metadata; the deploy left a stale checksum behind"
            )


@dataclass
class PruneResult:
    """What ``prune`` removed and what remains to publish."""

    removed: list[str]
    removed_files: int
    kept: int


def prune_metadata(m2repo: Path, baseline: Path) -> PruneResult:
    """Delete m2repo metadata still byte-identical to the baseline."""
    if not (baseline / BASELINE_MARKER).is_file():
        raise ActionError(f"{baseline} holds no fetch baseline; run mode 'fetch' first")
    removed: list[str] = []
    removed_files = 0
    for seeded in sorted(baseline.rglob(METADATA)):
        relative = seeded.relative_to(baseline).as_posix()
        target = safe_target(m2repo, relative)
        present = target.is_file()
        if present and target.read_bytes() != seeded.read_bytes():
            # The deploy rewrote it: this is the metadata to publish, so
            # its checksums must describe the new body, not the seeded one
            _verify_siblings(m2repo, relative, target.read_bytes())
            continue
        # Unchanged, or gone because the build removed it. Either way no
        # metadata is published here, so its checksums and signature
        # must not be either, or they would overwrite the server's.
        if present:
            target.unlink()
            removed_files += 1
        for extension in PRUNED_SIBLINGS:
            sibling = safe_target(m2repo, relative + extension)
            if sibling.is_file():
                sibling.unlink()
                removed_files += 1
        removed.append(relative)
    kept = sum(1 for _ in m2repo.rglob(METADATA)) if m2repo.is_dir() else 0
    return PruneResult(removed=removed, removed_files=removed_files, kept=kept)
