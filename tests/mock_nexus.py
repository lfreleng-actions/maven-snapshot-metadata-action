#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Minimal mock of a Nexus repository for action testing.

Serves ``GET`` requests from a directory tree laid out like a Maven
repository, under either the Nexus 2 prefix
(``/content/repositories/<repo>/``) or the Nexus 3 prefix
(``/repository/<repo>/``). A missing file answers 404.

Tests script failures through ``MockNexus.responses``: a mapping from
a path suffix to a list of responses served on successive requests for
a matching path, the last one repeating. An entry is an HTTP status
code, ``"drop"`` (close the connection without replying) or
``"redirect"`` (a 302 to another host).

Every request is recorded, with whether it carried an Authorization
header and whether that header matched ``expected_auth``; the header
value itself is never recorded.

Run as a script, it serves ``MOCK_ROOT`` and writes its port to
``MOCK_PORT_FILE``, which is how the end-to-end workflow uses it.
"""

from __future__ import annotations

import os
import sys
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import final

if __package__ in {None, ""}:
    # Run as a script (the end-to-end workflow does): make the
    # repository root importable so the package path below resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.compat import override  # noqa: E402

PREFIXES = ("/content/repositories/", "/repository/")


@dataclass
class Request:
    """One request the mock received."""

    path: str
    auth: bool
    auth_ok: bool


@dataclass
class MockNexus:
    """A threaded mock Nexus serving ``root`` on 127.0.0.1."""

    root: Path
    expected_auth: str | None = None
    responses: dict[str, list[int | str]] = field(default_factory=dict)
    requests: list[Request] = field(default_factory=list)
    _hits: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))
    _server: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        """Port the running server listens on."""
        assert self._server is not None
        return int(self._server.server_address[1])

    @property
    def url(self) -> str:
        """Base URL of the running server."""
        return f"http://127.0.0.1:{self.port}"

    def scripted(self, path: str) -> int | str | None:
        """Next scripted response for ``path``, if one matches."""
        for suffix, plan in self.responses.items():
            if path.endswith(suffix):
                index = min(self._hits[suffix], len(plan) - 1)
                self._hits[suffix] += 1
                return plan[index]
        return None

    def resolve(self, path: str) -> Path | None:
        """Map a request path onto a file under ``root``."""
        for prefix in PREFIXES:
            if path.startswith(prefix):
                _, _, rest = path[len(prefix) :].partition("/")
                target = (self.root / rest).resolve()
                if self.root.resolve() in target.parents and target.is_file():
                    return target
        return None

    def start(self) -> MockNexus:
        """Start serving in a daemon thread."""
        mock = self

        @final
        class Handler(BaseHTTPRequestHandler):
            @override
            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                del format, args

            def do_GET(self) -> None:  # noqa: N802 (http.server's name)
                header = self.headers.get("Authorization")
                mock.requests.append(
                    Request(
                        path=self.path,
                        auth=header is not None,
                        auth_ok=header == mock.expected_auth,
                    )
                )
                action = mock.scripted(self.path)
                if action == "drop":
                    self.close_connection = True
                    return
                if action == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://elsewhere.invalid/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if isinstance(action, int):
                    self._reply(action, b"")
                    return
                if mock.expected_auth and header != mock.expected_auth:
                    self._reply(401, b"")
                    return
                target = mock.resolve(self.path)
                if target is None:
                    self._reply(404, b"")
                else:
                    self._reply(200, target.read_bytes())

            def _reply(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                _ = self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        """Stop the server."""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def main() -> None:
    """Serve MOCK_ROOT until killed, writing the port to MOCK_PORT_FILE."""
    mock = MockNexus(root=Path(os.environ["MOCK_ROOT"])).start()
    port_file = Path(os.environ["MOCK_PORT_FILE"])
    _ = port_file.write_text(f"{mock.port}\n", encoding="utf-8")
    _ = threading.Event().wait()


if __name__ == "__main__":
    main()
