#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Tests manifest-guard.sh against scratch repositories, using whichever
# awk comes first on PATH. CI runs it on Linux and macOS, whose awks
# differ: macOS ships the BSD awk, which refuses some input other awks
# accept.
#
# Usage: sh .github/hooks/manifest-guard-test.sh

set -eu

guard="$(cd "$(dirname "$0")" && pwd)/manifest-guard.sh"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
failures=0

echo "awk: $(command -v awk)"

# A fresh repository holding the given paths in its index, which is all
# the guard reads. Each file is empty: only the paths matter.
repo() {
    dir=$(mktemp -d "$work/repo.XXXXXX")
    git -c init.defaultBranch=main init -q "$dir"
    for file in "$@"; do
        mkdir -p "$dir/$(dirname "$file")"
        : > "$dir/$file"
    done
    git -C "$dir" add -A
    printf '%s\n' "$dir"
}

fail() {
    echo "FAIL: $1"
    sed 's/^/  /' "$work/stderr"
    failures=$((failures + 1))
}

# expect_exit <code> <label> <repo> [guard arguments...]
expect_exit() {
    want=$1 label=$2 dir=$3
    shift 3
    got=0
    (cd "$dir" && sh "$guard" "$@") > /dev/null 2> "$work/stderr" || got=$?
    if [ "$got" -eq "$want" ]; then
        echo "ok: $label"
    else
        fail "$label: exit $got, expected $want"
    fi
}

# expect_list <expected, one path per line> <label> <repo> [arguments...]
expect_list() {
    want=$1 label=$2 dir=$3
    shift 3
    got=0
    (cd "$dir" && sh "$guard" --list "$@") > "$work/list" 2> "$work/stderr" || got=$?
    listed=$(tr '\0' '\n' < "$work/list")
    if [ "$got" -ne 0 ]; then
        fail "$label: --list exited $got"
    elif [ "$listed" != "$want" ]; then
        printf 'listed:\n%s\nexpected:\n%s\n' "$listed" "$want" > "$work/stderr"
        fail "$label: wrong list"
    else
        echo "ok: $label"
    fi
}

expect_exit 0 'no manifest and no sources' \
    "$(repo README.md)" package.json '*.ts'
expect_exit 3 'a root manifest owning a source' \
    "$(repo package.json src/a.ts)" package.json '*.ts'
expect_exit 4 'a root manifest owning no source' \
    "$(repo package.json)" package.json '*.ts'
expect_exit 1 'sources and no manifest' \
    "$(repo src/a.ts)" package.json '*.ts'
expect_exit 0 'every source under one nested manifest' \
    "$(repo a/package.json a/x.ts)" package.json '*.ts'

# Two or more nested manifests give the guard a multi-line list of
# directories to match against, which the BSD awk refused as a -v value.
nested="a/package.json a/x.ts b/package.json b/y.ts"
# shellcheck disable=SC2086 # deliberate word splitting into paths
expect_exit 3 'a root manifest beside two nested ones' \
    "$(repo package.json src/a.ts $nested)" package.json '*.ts'
# shellcheck disable=SC2086
expect_exit 4 'a root manifest whose sources are all nested' \
    "$(repo package.json $nested)" package.json '*.ts'
# shellcheck disable=SC2086
expect_exit 1 'a source outside two nested manifests, no root' \
    "$(repo src/a.ts $nested)" package.json '*.ts'
expect_list './src/a.ts' 'lists only the sources the root owns' \
    "$(repo package.json src/a.ts 'with space/package.json' 'with space/z.ts' a/package.json a/x.ts)" \
    package.json '*.ts'

if [ "$failures" -ne 0 ]; then
    echo "$failures case(s) failed"
    exit 1
fi
echo 'All cases passed'
