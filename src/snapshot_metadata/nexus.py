# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Read-only access to a Nexus 2 or Nexus 3 Maven repository."""

from __future__ import annotations

import base64
import http.client
import re
import ssl
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from snapshot_metadata import ActionError
from snapshot_metadata.workflow import emit, single_line

MAX_BODY = 8 * 1024 * 1024
MAX_RETRY_DELAY = 60
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
LAYOUTS = {"2": "content/repositories", "3": "repository"}


def repository_base_url(server: str, repository: str, nexus_version: str) -> str:
    """Build the repository root URL for a Nexus 2 or Nexus 3 server."""
    try:
        # urlsplit rejects a malformed bracketed host (https://[bad),
        # and parses the port lazily, so a non-numeric or out-of-range
        # one raises on first access. Surface both here, not mid-fetch.
        parts = urllib.parse.urlsplit(server.strip().rstrip("/"))
        _ = parts.port
    except ValueError as exc:
        raise ActionError(f"nexus_server is not a valid URL: {exc}") from exc
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ActionError("nexus_server must be an http(s) URL")
    try:
        # The connection IDNA-encodes the host, and a label longer than
        # 63 characters or an empty one raises UnicodeError there,
        # which is not an OSError; check it now, as input
        _ = parts.hostname.encode("idna")
    except UnicodeError as exc:
        raise ActionError(f"nexus_server has an invalid hostname: {exc}") from exc
    if parts.username or parts.password:
        raise ActionError("nexus_server must not embed credentials")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK_HOSTS:
        raise ActionError("nexus_server must use https unless it is loopback")
    if parts.query or parts.fragment:
        raise ActionError("nexus_server must not carry a query or fragment")
    if not REPOSITORY_RE.fullmatch(repository) or repository in {".", ".."}:
        raise ActionError(f"repository_name {repository!r} is not valid")
    if nexus_version not in LAYOUTS:
        raise ActionError("nexus_version must be '2' or '3'")
    base = urllib.parse.urlunsplit(
        # http.client sends the path as ASCII and raises on anything
        # else, so encode a non-ASCII context path here, as a URL would
        parts._replace(path=urllib.parse.quote(parts.path, safe="/%"))
    )
    return f"{base}/{LAYOUTS[nexus_version]}/{repository}/"


def basic_auth(username: str, password: str) -> str | None:
    """Return a Basic Authorization header value, or None without both."""
    if not username and not password:
        return None
    if not username or not password:
        raise ActionError("supply nexus_username and nexus_password together")
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


@dataclass(frozen=True)
class Response:
    """The parts of an HTTP response the fetcher uses."""

    status: int
    body: bytes


def http_get(url: str, authorization: str | None, timeout: float) -> Response:
    """GET one URL, without following redirects.

    ``http.client`` rather than ``urllib``: urllib follows redirects
    and carries the Authorization header to wherever they point.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    connection: http.client.HTTPConnection
    if parts.scheme == "https":
        connection = http.client.HTTPSConnection(
            host, parts.port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection(host, parts.port, timeout=timeout)
    headers = {"User-Agent": "maven-snapshot-metadata-action", "Accept": "*/*"}
    if authorization:
        headers["Authorization"] = authorization
    try:
        connection.request("GET", parts.path or "/", headers=headers)
        response = connection.getresponse()
        return Response(response.status, response.read(MAX_BODY + 1))
    finally:
        connection.close()


Getter = Callable[[str, "str | None", float], Response]


class Fetcher:
    """Fetch repository files with bounded retries, failing closed.

    404 means "not published yet". Transient failures (network errors,
    HTTP 429 and 5xx) retry with doubling delays. Everything else, and
    a transient failure outlasting every attempt, raises ActionError.
    """

    def __init__(
        self,
        authorization: str | None,
        attempts: int,
        retry_delay: int,
        timeout: float = 30.0,
        get: Getter = http_get,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._authorization: str | None = authorization
        self._attempts: int = attempts
        self._retry_delay: int = retry_delay
        self._timeout: float = timeout
        self._get: Getter = get
        self._sleep: Callable[[float], None] = sleep
        self.requests: int = 0

    def _classify(self, url: str, response: Response) -> bytes | None | str:
        """Return the body, None for 404, or a retry reason."""
        status = response.status
        if status == 200:
            if len(response.body) > MAX_BODY:
                raise ActionError(f"{url} exceeds {MAX_BODY} bytes")
            return response.body
        if status == 404:
            return None
        if status in {401, 403}:
            raise ActionError(
                f"HTTP {status} for {url}: check the repository credentials"
            )
        if 300 <= status < 400:
            raise ActionError(
                f"HTTP {status} redirect for {url}: point nexus_server at"
                + " the canonical https URL"
            )
        if status != 429 and status < 500:
            raise ActionError(f"HTTP {status} for {url}")
        return f"HTTP {status}"

    def fetch(self, url: str) -> bytes | None:
        """Return the body, or None when the server reports 404."""
        reason = ""
        for attempt in range(1, self._attempts + 1):
            self.requests += 1
            try:
                outcome = self._classify(
                    url, self._get(url, self._authorization, self._timeout)
                )
            except ssl.SSLCertVerificationError as exc:
                raise ActionError(f"TLS verification failed for {url}") from exc
            except (OSError, http.client.HTTPException) as exc:
                outcome = single_line(f"{type(exc).__name__}: {exc}")
            if not isinstance(outcome, str):
                return outcome
            reason = outcome
            if attempt < self._attempts:
                delay = min(self._retry_delay << (attempt - 1), MAX_RETRY_DELAY)
                emit(
                    f"Retrying {url} ({reason}), attempt {attempt + 1}"
                    + f"/{self._attempts} in {delay}s"
                )
                self._sleep(delay)
        raise ActionError(f"{url} failed after {self._attempts} attempts: {reason}")
