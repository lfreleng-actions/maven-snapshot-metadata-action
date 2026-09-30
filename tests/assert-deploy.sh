#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Assert the state a merge lane depends on, after fetch, deploy, prune.
#
# Usage: assert-deploy.sh <m2repo> <groupId> <deployed-artifact> \
#          <skipped-artifact> <seeded-build-number>
#
# The fixture seeds version-level SNAPSHOT metadata for two modules at
# <seeded-build-number>, then deploys only <deployed-artifact>. Proves:
#   1. the deploy continued the published sequence (seeded + 1), which
#      happens only when fetch put the metadata where Maven reads it;
#   2. the deployed module's artefacts carry that build number;
#   3. prune removed the untouched module's seeded metadata, so it
#      cannot overwrite a newer copy on the server.

set -euo pipefail

m2repo="${1:?m2repo required}"
group_path="${2:?groupId required}"
group_path="${group_path//.//}"
deployed="${3:?deployed artifact required}"
skipped="${4:?skipped artifact required}"
seeded="${5:?seeded build number required}"
version="1.0.0-SNAPSHOT"
status=0

fail() {
  echo "::error::$1"
  status=1
}

deployed_dir="${m2repo}/${group_path}/${deployed}/${version}"
metadata="${deployed_dir}/maven-metadata.xml"
expected=$((seeded + 1))
if [ ! -f "${metadata}" ]; then
  fail "${deployed}: no version-level metadata after deploy"
else
  actual="$(sed -n 's:.*<buildNumber>\([0-9]*\)</buildNumber>.*:\1:p' \
    "${metadata}" | head -n 1)"
  if [ "${actual}" = "${expected}" ]; then
    echo "${deployed}: buildNumber ${seeded} -> ${actual} ✅"
  else
    fail "${deployed}: buildNumber '${actual}', expected ${expected}"
  fi
fi

jars="$(find "${deployed_dir}" -maxdepth 1 \
  -name "${deployed}-1.0.0-*-${expected}.jar" 2> /dev/null || true)"
if [ -n "${jars}" ]; then
  echo "${deployed}: artefact $(basename "${jars}") ✅"
else
  fail "${deployed}: no jar carrying build ${expected}"
fi

skipped_metadata="${m2repo}/${group_path}/${skipped}/${version}/maven-metadata.xml"
if [ -e "${skipped_metadata}" ]; then
  fail "${skipped}: seeded metadata survived prune"
else
  echo "${skipped}: untouched metadata pruned ✅"
fi

exit "${status}"
