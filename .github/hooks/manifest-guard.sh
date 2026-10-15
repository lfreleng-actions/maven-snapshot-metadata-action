#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Decides whether a language's root-level tooling applies to this checkout.
#
# Usage: manifest-guard.sh [--list] <manifest> <source-pathspec>...
#   GUARD_EXCLUDE  optional extended regex of source paths to ignore
#
# With --list it prints the tracked sources the root manifest owns as a
# NUL-delimited stream, for `xargs -0`, and exits 0. It is not
# line-oriented: a caller reading it as lines would split any path
# containing whitespace. A formatter needs that list because it rewrites
# files directly: `golangci-lint fmt` walks every Go file beneath the
# working directory and does not stop at module boundaries, so on a
# repository holding both a root and a nested module it would reformat
# the nested module with the root settings while `go mod tidy` updated
# only the root, leaving the nested module stale. Passing the owned set
# explicitly keeps the formatter inside the module it belongs to.
#
# Exit codes:
#   3  a root manifest is tracked and owns at least one source: run the tool
#   4  a root manifest is tracked but owns no source. Tools that only touch
#      the manifest, or that are no-ops without sources, should still run;
#      analysis tools must skip, because `go vet ./...` exits 1 and
#      `golangci-lint run` exits 5 on a module with nothing to analyse, which
#      would block the very commit that removes the last Go file.
#   0  the language is absent, or every tracked source lives under a manifest
#      in a subdirectory, so the root-level hook is inert
#   1  tracked sources exist that no tracked manifest covers
#
# Paths come from git with core.quotePath disabled, because the default
# C-quotes anything non-ASCII: a tracked tools/café/go.mod would arrive
# wrapped in quotes and match no prefix, so its sources would be reported
# as uncovered. A tracked path containing a literal newline is still not
# supported; git quotes those regardless, and the quoted form matches no
# prefix, so such a file is reported as uncovered and the hook fails
# rather than skipping silently.
#
# Every test is against the git index rather than the filesystem. A
# pre-commit hook validates what is being committed, so an untracked
# manifest sitting on disk must not satisfy it: a commit carrying sources
# but not their manifest would otherwise pass locally and land a tree
# nobody else can build.
#
# One computation serves both questions. "Unnested" below is the set of
# tracked sources that do not sit under a manifest in a subdirectory. When a
# root manifest is tracked those are the sources it owns; when none is
# tracked they are sources no manifest covers at all. Deciding coverage per
# file, rather than asking whether a nested manifest exists anywhere, is what
# stops a nested module masking deletion of the root manifest while sources
# outside it go unlinted.

set -eu

list_only=0
if [ "${1:-}" = "--list" ]; then
    list_only=1
    shift
fi

if [ "$#" -lt 2 ]; then
    echo "usage: manifest-guard.sh [--list] <manifest> <source-pathspec>..." >&2
    exit 2
fi

manifest=$1
shift

# core.quotePath=false only stops git quoting non-ASCII bytes. A path
# holding a double quote, backslash, tab or newline is C-quoted regardless,
# and the quoted form matches no prefix and cannot be opened by a formatter.
# NUL-delimited matching would handle these, but the awk available on macOS
# has no NUL record separator and POSIX sh has no `read -d`, so there is no
# portable way to compare them correctly. Refuse them with a diagnostic
# instead of proceeding on paths that cannot be handled.
quoted=$(git -c core.quotePath=false ls-files -- "$manifest" "*/$manifest" "$@" \
    | grep -m1 '^"' || true)
if [ -n "$quoted" ]; then
    echo "manifest-guard: cannot handle the tracked path $quoted" >&2
    echo "  rename it: paths containing a quote, backslash, tab or newline" >&2
    echo "  are quoted by git and cannot be matched or passed on safely" >&2
    exit 1
fi

root_tracked=0
if git -c core.quotePath=false ls-files -- "$manifest" | grep -q .; then
    root_tracked=1
fi

sources=$(git -c core.quotePath=false ls-files -- "$@")
if [ -n "${GUARD_EXCLUDE:-}" ]; then
    sources=$(printf '%s\n' "$sources" | grep -vE "$GUARD_EXCLUDE" || true)
fi

# Directories holding a tracked manifest below the root.
dirs=$(git -c core.quotePath=false ls-files -- "*/$manifest" | sed "s|/$manifest\$||" || true)

# The first tracked source not sitting under one of those directories.
# The list reaches awk through the environment: macOS awk refuses a
# newline in a -v value, which two nested manifests produce.
owned=""
if [ -n "$sources" ]; then
    owned=$(printf '%s\n' "$sources" | GUARD_DIRS=$dirs awk '
        BEGIN { n = split(ENVIRON["GUARD_DIRS"], d, "\n") }
        {
            for (i = 1; i <= n; i++)
                if (d[i] != "" && index($0, d[i] "/") == 1)
                    next
            print
        }')
fi

if [ "$list_only" = 1 ]; then
    # NUL-delimited so a caller can pipe straight into `xargs -0`.
    # './' prefix: a tracked source such as -flag.go would otherwise be
    # read as an option by the tool xargs hands it to.
    [ -n "$owned" ] && printf '%s\n' "$owned" | sed 's|^|./|' | tr '\n' '\0'
    exit 0
fi

# Only the first is needed for the verdict, and it names the offender.
unnested=$(printf '%s\n' "$owned" | sed -n '1p')

if [ "$root_tracked" = 1 ]; then
    [ -n "$unnested" ] && exit 3
    exit 4
fi

# No root manifest from here on.
[ -n "$sources" ] || exit 0

if [ -n "$unnested" ]; then
    echo "no tracked $manifest covers $unnested - restore or stage $manifest" >&2
    exit 1
fi

exit 0
