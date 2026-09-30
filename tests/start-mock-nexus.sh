#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Start the mock Nexus in the background and wait until it listens.
#
# Usage: start-mock-nexus.sh <server-root> <port-file>
#
# Prints the server's base URL. The port file appears only once the
# socket is listening, so waiting for it (rather than sleeping a fixed
# time) is what keeps this reliable on a slow or busy runner.

set -euo pipefail

root="${1:?server root required}"
port_file="${2:?port file required}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

rm -f "${port_file}"
MOCK_ROOT="${root}" MOCK_PORT_FILE="${port_file}" \
  nohup python3 "${here}/mock_nexus.py" > "${port_file}.log" 2>&1 &

for _ in $(seq 1 100); do
  if [ -s "${port_file}" ]; then
    printf 'http://127.0.0.1:%s\n' "$(tr -d '[:space:]' < "${port_file}")"
    exit 0
  fi
  sleep 0.1
done
echo "mock Nexus did not start within 10 seconds:" >&2
cat "${port_file}.log" >&2
exit 1
