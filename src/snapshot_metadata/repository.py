# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Seeding and pruning metadata in the local deploy repository."""

from __future__ import annotations

import hashlib
import os
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
# Removed alongside a pruned metadata file, whichever exist: its
# checksums, its detached signature, and the signature's checksums.
DIGEST_SUFFIXES = (".md5", ".sha1", ".sha256", ".sha512")
PRUNED_SIBLINGS = (
    *DIGEST_SUFFIXES,
    ".asc",
    *(f".asc{suffix}" for suffix in DIGEST_SUFFIXES),
)
BASELINE_MARKER = ".maven-snapshot-metadata-baseline"


def looks_like_metadata(body: bytes) -> bool:
    """Whether a response body is Maven repository metadata.

    Parsed rather than prefix-matched: an XML error page or login
    response also starts with a declaration. Only a ``<metadata>``
    root qualifies. The body comes from the configured server, is
    capped at MAX_BODY, and ElementTree resolves no external entities.
    A declared encoding expat cannot use fails with LookupError or
    ValueError, not ParseError, and is just as invalid.
    """
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, LookupError, ValueError):
        return False
    return root.tag.rsplit("}", 1)[-1] == "metadata"


def checksum_matches(sidecar: bytes, body: bytes, extension: str) -> bool:
    """Whether a checksum file holds the digest of ``body``.

    Recomputed rather than checked for shape: a well-formed but stale
    sidecar, fetched after the metadata changed on the server, would
    otherwise ship and fail Maven's verification. Some tools append a
    filename after the digest, so only the first word counts. Declared
    a non-security use: it checks Maven's legacy checksum, and a FIPS
    build of OpenSSL refuses MD5 for anything else.
    """
    try:
        words = sidecar.decode("ascii").split()
    except UnicodeDecodeError:
        return False
    algorithm = SEEDED_CHECKSUMS[extension]
    digest = hashlib.new(algorithm, body, usedforsecurity=False).hexdigest()
    return bool(words) and words[0].lower() == digest


def resolve_path(path: Path) -> Path:
    """``path.resolve()``, with a symbolic-link loop as an ActionError.

    Python 3.12 and older raise RuntimeError for a loop, which would
    escape main()'s handlers as a traceback; 3.13 resolves it as is.
    """
    try:
        return path.resolve()
    except RuntimeError as exc:
        raise ActionError(f"{path} is in a symbolic link loop") from exc


def safe_target(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, refusing any escape.

    Relative paths come from validated coordinates, so this guards
    against a symlinked directory the build left inside the tree.
    """
    target = root / relative
    resolved_root = resolve_path(root)
    resolved = resolve_path(target)
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


def _reraise(exc: OSError) -> None:
    raise exc


def _refuse_links(root: Path) -> None:
    """Fail on the first symbolic link beneath ``root``.

    os.walk lists a linked directory without entering it, so the scan
    sees the link itself; rglob would skip it and never look inside. A
    directory it cannot list fails the scan, rather than hiding a link.
    """
    for directory, dirs, files in os.walk(root, onerror=_reraise):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(directory, name)
            if path.is_symlink():
                raise ActionError(
                    f"{root} holds a symbolic link at"
                    + f" {path.relative_to(root).as_posix()}, which the deploy"
                    + " could follow out of it; fetch needs a clean m2repo"
                )


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
    Nor may it hold a symbolic link, to a file or a directory, dangling
    or not: where the server has nothing, fetch writes nothing through
    it, and the deploy would follow it out of the tree that publishes.
    The m2repo itself may be a link; only what lies beneath it counts.

    The baseline marker goes in last, once every path has seeded, so an
    interrupted fetch leaves a baseline ``prune`` refuses. It names the
    m2repo seeded, so ``prune`` also refuses to judge any other.
    """
    if m2repo.is_dir():
        _refuse_links(resolve_path(m2repo))
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
    _ = (baseline / BASELINE_MARKER).write_bytes(_marker(m2repo))
    return FetchResult(metadata=seeded, files=files)


def _marker(m2repo: Path) -> bytes:
    return os.fsencode(resolve_path(m2repo)) + b"\n"


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
    """Delete m2repo metadata still byte-identical to the baseline.

    Only in the m2repo fetch seeded: anywhere else every baseline file
    would be missing, so all would count as pruned while the seeded
    copies went on to publish.
    """
    marker = baseline / BASELINE_MARKER
    if not marker.is_file():
        raise ActionError(f"{baseline} holds no fetch baseline; run mode 'fetch' first")
    recorded = marker.read_bytes()
    if recorded != _marker(m2repo):
        raise ActionError(
            f"{baseline} was fetched into {os.fsdecode(recorded).rstrip()},"
            + f" not {resolve_path(m2repo)}; give fetch and prune the same m2repo_path"
        )
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
