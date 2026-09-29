# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""GitHub Actions plumbing: logging, outputs and summaries."""

from __future__ import annotations

import os
import secrets
import sys


def emit(line: str) -> None:
    """Write one line to the job log, flushed at once.

    stdout is this step's interface, not a debug channel: the runner
    reads workflow commands such as ``::error::`` and ``::add-mask::``
    from it, so they cannot go through a logger that might reformat
    or redirect them. Piped stdout is block-buffered, so each line is
    flushed: a mask must reach the runner before what it hides, and a
    retry notice must survive a step killed mid-retry.
    """
    _ = sys.stdout.write(f"{line}\n")
    _ = sys.stdout.flush()


def single_line(text: str) -> str:
    """Flatten untrusted text onto one line for a plain log message.

    Exception text can carry a server's own line breaks (http.client's
    BadStatusLine keeps the raw status line), and a second line such as
    '::set-output ...' would reach the runner as a workflow command.
    """
    return " ".join(text.splitlines())


def escape_command_data(text: str) -> str:
    """Escape text for the data part of a workflow command.

    Error messages can quote effective-POM values or URLs. Unescaped,
    a line break in one would start a second workflow command.
    """
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def emit_error(message: str) -> None:
    """Report an error annotation, escaped so it stays one command."""
    emit(f"::error::{escape_command_data(message)}")


def mask(secret: str) -> None:
    """Ask the runner to redact every line of a secret from the log.

    The runner decodes command data before registering the mask, so a
    secret containing '%25' would otherwise mask '%' instead of itself.
    """
    for line in secret.splitlines():
        if line:
            emit(f"::add-mask::{escape_command_data(line)}")


def emit_untrusted(text: str) -> None:
    """Log build output with workflow commands suspended.

    The output comes from the project's build, so a line such as
    '::add-mask::' must not reach the runner as a command.
    """
    token = secrets.token_hex(16)
    emit(f"::stop-commands::{token}")
    emit(text.rstrip())
    emit(f"::{token}::")


def write_outputs(values: dict[str, str]) -> None:
    """Append step outputs, each with a delimiter absent from its value."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            delimiter = f"EOF_{secrets.token_hex(16)}"
            while delimiter in value:
                delimiter = f"EOF_{secrets.token_hex(16)}"
            _ = handle.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")


def write_summary(lines: list[str]) -> None:
    """Append Markdown to the job summary when one is available."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            _ = handle.write("\n".join(lines) + "\n")
