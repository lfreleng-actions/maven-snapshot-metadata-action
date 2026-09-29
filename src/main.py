#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Run the action.

Invoked as a script, never with ``python3 -m``. A script puts its own
directory first on ``sys.path``; ``-m`` puts the current directory
there instead, and in a composite action that is the caller's
checkout, where a ``xml/`` or ``ssl.py`` would shadow the standard
library.
"""

import sys

from snapshot_metadata.cli import main

if __name__ == "__main__":
    sys.exit(main())
