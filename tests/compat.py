# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""``typing.override``, with a no-op before Python 3.12.

Type checkers see the real decorator. At run time an interpreter older
than 3.12, such as a contributor's local one, gets a stand-in that
returns the method unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import override
else:
    try:
        from typing import override
    except ImportError:

        def override(func):
            """Mark a method as overriding one in a base class."""
            return func


__all__ = ["override"]
