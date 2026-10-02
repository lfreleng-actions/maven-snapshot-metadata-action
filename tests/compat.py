# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""``typing.override``, with a no-op before Python 3.12.

Type checkers see the real decorator. At run time an interpreter older
than 3.12, such as a contributor's local one, gets a stand-in that
returns the method unchanged.

The checkers take it from ``typing_extensions``, whose stubs they
bundle, so it resolves when they target the 3.9 floor. That import is
never executed, so nothing needs installing.
"""

# The import below is resolved from basedpyright's bundled stubs, so it
# reports the package source as missing wherever typing_extensions is
# not installed, as in the pre-commit hook. That is harmless: the import
# runs only under TYPE_CHECKING. A per-line ignore would instead be
# flagged as unnecessary wherever the package is installed.
# pyright: reportMissingModuleSource=false

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing_extensions import override
else:
    try:
        from typing import override
    except ImportError:

        def override(func):
            """Mark a method as overriding one in a base class."""
            return func


__all__ = ["override"]
