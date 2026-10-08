# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the src/snapshot_metadata package, against a mock Nexus."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
import json
import os
import pathlib
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import cast
from unittest import mock

from snapshot_metadata import ActionError, cli
from snapshot_metadata.coordinates import (
    ACTION_INPUT_PREFIX,
    ACTION_INPUT_VARIABLES,
    HELP_PLUGIN,
    Coordinate,
    discover_coordinates,
    maven_environment,
    parse_active_profiles,
    top_level_group_paths,
)
from snapshot_metadata.maven_args import long_option_names, split_maven_args
from snapshot_metadata.nexus import (
    Fetcher,
    Response,
    basic_auth,
    repository_base_url,
)
from snapshot_metadata.repository import (
    BASELINE_MARKER,
    FetchResult,
    checksum_matches,
    prune_metadata,
    seed_metadata,
)
from snapshot_metadata.workflow import escape_command_data, mask, write_outputs
from tests.compat import override
from tests.mock_nexus import MockNexus

GROUP = "org/example"
CORE = f"{GROUP}/core"
CORE_V = f"{CORE}/1.0.0-SNAPSHOT"
# A coordinate outside the reactor, as a POM-bound deploy-file writes it
EXTRA_V = f"{GROUP}/extra/1.0.0-SNAPSHOT"
# Where fetch records the reactor it read, beside the baseline marker
COORDINATES_RECORD = ".maven-snapshot-metadata-coordinates.json"


def digest(body: bytes, algorithm: str) -> bytes:
    """A checksum file holding the true digest of ``body``."""
    return hashlib.new(algorithm, body, usedforsecurity=False).hexdigest().encode()


def metadata(build: int) -> bytes:
    """Version-level SNAPSHOT metadata at a given buildNumber."""
    return textwrap.dedent(
        f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <metadata modelVersion="1.1.0">
          <groupId>org.example</groupId>
          <artifactId>core</artifactId>
          <version>1.0.0-SNAPSHOT</version>
          <versioning>
            <snapshot><timestamp>20260925.120000</timestamp>
              <buildNumber>{build}</buildNumber></snapshot>
          </versioning>
        </metadata>
        """
    ).encode()


ARTIFACT_METADATA = b'<?xml version="1.0"?><metadata><versioning/></metadata>\n'


def coordinate(artifact: str = "core", packaging: str = "jar") -> Coordinate:
    return Coordinate("org.example", artifact, "1.0.0-SNAPSHOT", packaging)


def record(artifact: str = "core", packaging: str = "jar") -> dict[str, str]:
    """One module as fetch records it."""
    return {
        "groupId": "org.example",
        "artifactId": artifact,
        "version": "1.0.0-SNAPSHOT",
        "packaging": packaging,
    }


def read_json(path: Path) -> object:
    """A JSON file, parsed for the assertions to compare."""
    return cast(object, json.loads(path.read_text(encoding="utf-8")))


def read_outputs(path: Path) -> dict[str, str]:
    """Step outputs, as write_outputs appends them to GITHUB_OUTPUT."""
    values: dict[str, str] = {}
    lines = iter(path.read_text(encoding="utf-8").splitlines())
    for line in lines:
        key, _, delimiter = line.partition("<<")
        value: list[str] = []
        for item in lines:
            if item == delimiter:
                break
            value.append(item)
        values[key] = "\n".join(value)
    return values


class TempTestCase(unittest.TestCase):
    """Provides a temporary directory, removed after each test."""

    # Replaced in setUp; the defaults keep every fixture initialised
    tmp: Path = Path()

    @override
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, root: Path, relative: str, content: bytes) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_bytes(content)
        return path


class NexusTestCase(TempTestCase):
    """Runs a mock Nexus over a temporary repository tree."""

    server_root: Path = Path()
    m2repo: Path = Path()
    baseline: Path = Path()
    mock: MockNexus = MockNexus(root=Path())
    sleeps: tuple[float, ...] = ()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.server_root = self.tmp / "server"
        self.server_root.mkdir()
        self.m2repo = self.tmp / "m2repo"
        self.baseline = self.tmp / "baseline"
        self.mock = MockNexus(root=self.server_root).start()
        self.addCleanup(self.mock.stop)
        self.sleeps = ()

    def base_url(self, version: str = "2") -> str:
        return repository_base_url(self.mock.url, "snapshots", version)

    def fetcher(self, authorization: str | None = None, attempts: int = 3) -> Fetcher:
        return Fetcher(authorization, attempts, 1, timeout=5, sleep=self._record_sleep)

    def _record_sleep(self, delay: float) -> None:
        self.sleeps = (*self.sleeps, delay)

    def seed(
        self,
        coordinates: list[Coordinate],
        fetcher: Fetcher | None = None,
        base_url: str | None = None,
    ) -> FetchResult:
        return seed_metadata(
            coordinates,
            base_url or self.base_url(),
            fetcher or self.fetcher(),
            self.m2repo,
            self.baseline,
        )


class TestCoordinate(unittest.TestCase):
    def test_snapshot_jar_reads_artifact_and_version_metadata(self) -> None:
        self.assertEqual(
            coordinate().metadata_paths(),
            [f"{CORE}/maven-metadata.xml", f"{CORE_V}/maven-metadata.xml"],
        )

    def test_release_version_has_no_version_level_metadata(self) -> None:
        release = Coordinate("org.example", "core", "1.0.0", "jar")
        self.assertEqual(release.metadata_paths(), [f"{CORE}/maven-metadata.xml"])

    def test_maven_plugin_adds_group_level_metadata(self) -> None:
        paths = coordinate("tool", "maven-plugin").metadata_paths()
        self.assertIn(f"{GROUP}/maven-metadata.xml", paths)

    def test_top_level_group_paths_drop_nested_groups(self) -> None:
        coordinates = [
            Coordinate("org.example", "a", "1-SNAPSHOT", "jar"),
            Coordinate("org.example.sub", "b", "1-SNAPSHOT", "jar"),
            Coordinate("com.other", "c", "1-SNAPSHOT", "jar"),
        ]
        self.assertEqual(
            top_level_group_paths(coordinates), ["com/other", "org/example"]
        )


def listing(*ids: str) -> str:
    """What help:active-profiles prints for a reactor, as Maven 3.9 logs it."""
    blocks = [
        f"Active Profiles for Project '{i}':\n\nThere are no active profiles.\n\n\n"
        for i in ids
    ]
    return "[INFO] \n" + "".join(blocks) + "[INFO] BUILD SUCCESS\n"


def print_listing(*ids: str) -> str:
    """A fake mvn's shell lines printing ``listing(*ids)`` to stdout."""
    return f"cat <<'LISTING'\n{listing(*ids)}LISTING\n"


class TestActiveProfiles(unittest.TestCase):
    def test_reactor_with_inherited_coordinates_and_default_packaging(self) -> None:
        # Maven reports each project's effective model, so a module that
        # inherits its groupId and version is listed with both resolved
        coordinates = parse_active_profiles(
            listing(
                "org.example:parent:pom:1.0.0-SNAPSHOT",
                "org.example:core:jar:1.0.0-SNAPSHOT",
            )
        )
        self.assertEqual(
            coordinates,
            [
                Coordinate("org.example", "core", "1.0.0-SNAPSHOT", "jar"),
                Coordinate("org.example", "parent", "1.0.0-SNAPSHOT", "pom"),
            ],
        )

    def test_active_profiles_and_other_log_lines_are_ignored(self) -> None:
        text = (
            "[INFO] Scanning for projects...\n[INFO] \n"
            "Active Profiles for Project 'org.example:solo:jar:2.0-SNAPSHOT':\n\n"
            "The following profiles are active:\n\n"
            " - ci (source: org.example:solo:2.0-SNAPSHOT)\n\n"
        )
        coordinates = parse_active_profiles(text)
        self.assertEqual([c.artifact_id for c in coordinates], ["solo"])

    def test_colour_escapes_and_crlf_are_tolerated(self) -> None:
        text = "\x1b[1mActive Profiles for Project 'g:a:jar:1-SNAPSHOT':\x1b[m\r\n"
        self.assertEqual(
            parse_active_profiles(text), [Coordinate("g", "a", "1-SNAPSHOT", "jar")]
        )

    def test_no_listing_fails_closed(self) -> None:
        # -q, or an 'output' property redirecting the report to a file,
        # leaves Maven successful and stdout without a single project
        for text in ("", "[INFO] Active profile report written to: /x\n"):
            with (
                self.subTest(text=text),
                self.assertRaisesRegex(ActionError, "listed no projects.*-q.*output"),
            ):
                _ = parse_active_profiles(text)

    def test_unresolved_property_is_rejected_with_a_hint(self) -> None:
        with self.assertRaisesRegex(ActionError, "unresolved property"):
            _ = parse_active_profiles(listing("org.example:a:jar:${revision}"))

    def test_path_traversal_in_a_coordinate_is_rejected(self) -> None:
        with self.assertRaisesRegex(ActionError, "not a valid coordinate"):
            _ = parse_active_profiles(listing("org.example:..:jar:1-SNAPSHOT"))

    def test_an_uninherited_placeholder_is_rejected(self) -> None:
        with self.assertRaisesRegex(ActionError, "not a valid coordinate"):
            _ = parse_active_profiles(listing("[inherited]:a:jar:1-SNAPSHOT"))

    def test_a_project_id_without_four_fields_is_rejected(self) -> None:
        for project_id in ("g:a:1-SNAPSHOT", "g:a:jar:x:1-SNAPSHOT"):
            with (
                self.subTest(project_id=project_id),
                self.assertRaisesRegex(ActionError, "unexpected project id"),
            ):
                _ = parse_active_profiles(listing(project_id))


class TestMavenArgs(unittest.TestCase):
    def test_file_selection_is_rejected_in_every_spelling(self) -> None:
        for arg in ("-f", "-fother.xml", "-f=other.xml", "--file", "--file=x.xml"):
            with self.subTest(arg=arg), self.assertRaises(ActionError):
                _ = split_maven_args(f"-q {arg}")

    def test_reactor_narrowing_is_rejected_in_every_spelling(self) -> None:
        for arg in (
            "-pl",
            "-plcore",
            "--projects",
            "--projects=core",
            "-N",
            "--non-recursive",
            "-rf",
            "-rf:core",
            "--resume-from=core",
            "-am",
            "--also-make",
            "-amd",
            "--also-make-dependents",
            "-r",
            "--resume",
        ):
            with (
                self.subTest(arg=arg),
                self.assertRaisesRegex(ActionError, "narrows the reactor"),
            ):
                _ = split_maven_args(f"-q {arg}")

    def test_reading_arguments_from_a_file_is_rejected(self) -> None:
        for arg in ("-af", "-afargs.txt", "--at-file", "--at-file=args.txt"):
            with (
                self.subTest(arg=arg),
                self.assertRaisesRegex(ActionError, "unchecked arguments from a file"),
            ):
                _ = split_maven_args(f"-q {arg}")

    def test_goals_and_phases_are_rejected(self) -> None:
        for args in (
            "deploy",
            "clean install",
            "-s settings.xml deploy",
            "org.example:plugin:1.0:goal",
        ):
            with (
                self.subTest(args=args),
                self.assertRaisesRegex(ActionError, "not goals or phases"),
            ):
                _ = split_maven_args(args)

    def test_an_option_missing_its_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ActionError, "missing its value"):
            _ = split_maven_args("-B -s")

    def test_options_with_separate_values_pass(self) -> None:
        args = "-s settings.xml -P ci -D revision=1-SNAPSHOT -T 4"
        self.assertEqual(len(split_maven_args(args)), 8)

    def test_short_fail_on_severity_is_rejected_as_a_pom_selector(self) -> None:
        # Maven 3 has no -fos: it reads '-fosWARN' as '-f osWARN', and
        # '-fosa/../pom.xml' as a working '-f osa/../pom.xml'
        for arg in ("-fos", "-fosWARN", "-fosa/../pom.xml"):
            with (
                self.subTest(arg=arg),
                self.assertRaisesRegex(ActionError, "selects a POM"),
            ):
                _ = split_maven_args(arg)

    def test_long_fail_on_severity_passes(self) -> None:
        for args in ("--fail-on-severity WARN", "--fail-on-severity=ERROR"):
            with self.subTest(args=args):
                _ = split_maven_args(args)

    def test_abbreviated_long_options_are_rejected(self) -> None:
        # Maven 4 accepts any unambiguous prefix: in a real run, --non-r
        # narrowed the reactor to one module and --resume-f=core to three
        for arg in (
            "--non-r",
            "--resume-f=core",
            "--proje=core",
            "--also-m",
            "--also-make-d",
            "--at-f=args.txt",
            "--fil=other.xml",
        ):
            with (
                self.subTest(arg=arg),
                self.assertRaisesRegex(
                    ActionError,
                    "; it (narrows the reactor|reads unchecked|selects a POM)",
                ),
            ):
                _ = split_maven_args(arg)

    def test_options_sharing_a_prefix_with_forbidden_ones_pass(self) -> None:
        for args in (
            "--fail-on-severity WARN",
            "--fail-fast",
            "--fail-at-end",
            "--settings=s.xml",
            "--no-transfer-progress",
        ):
            with self.subTest(args=args):
                _ = split_maven_args(args)

    def test_abbreviations_of_safe_options_are_refused_too(self) -> None:
        # The allow-list takes each option spelled out: judging which
        # prefixes Maven resolves unambiguously is the guesswork it avoids
        with self.assertRaises(ActionError):
            _ = split_maven_args("--sett=s.xml")

    def test_short_option_clusters_are_refused(self) -> None:
        # Maven 4 bursts -qN into -q -N: in a real run it described one
        # module. Harmless clusters are refused as well, for simplicity
        for arg in ("-qN", "-Nq", "-BN", "-qBN", "-eN", "-qB", "-Bq"):
            with self.subTest(arg=arg), self.assertRaises(ActionError):
                _ = split_maven_args(arg)
        with self.assertRaisesRegex(ActionError, "narrows the reactor"):
            _ = split_maven_args("-qN")

    def test_single_hyphen_long_names_are_refused(self) -> None:
        # Maven 3 and 4 both read -non-recursive as --non-recursive, and
        # a real run described one module. Every long name is probed,
        # bare and with a value, so none can slip through as an
        # attached short value (-settings=x is not -s 'ettings=x')
        for name in sorted(long_option_names()):
            for arg in (name[1:], f"{name[1:]}=x"):
                with self.subTest(arg=arg), self.assertRaises(ActionError):
                    _ = split_maven_args(arg)

    def test_refused_modes_after_one_hyphen_are_refused(self) -> None:
        # '-shell' must not read as '-s' with 'hell': Maven 3 takes it as
        # a settings file, Maven 4 does not, and neither is fetch's call
        for arg in ("-shell", "-up", "-enc", "-yjp", "-help", "-version", "-debug"):
            with self.subTest(arg=arg), self.assertRaises(ActionError):
                _ = split_maven_args(arg)

    def test_attached_short_values_pass(self) -> None:
        for arg in ("-ss.xml", "-Dx=y", "-Pci,release", "-T4", "-bsmart", "-gsg.xml"):
            with self.subTest(arg=arg):
                _ = split_maven_args(arg)

    def test_color_takes_an_optional_value(self) -> None:
        for args in ("--color never", "--color", "--color -B", "--color=always -q"):
            with self.subTest(args=args):
                _ = split_maven_args(args)
        with self.assertRaisesRegex(ActionError, "not goals or phases"):
            _ = split_maven_args("--color never deploy")

    def test_failure_mode_flags_and_properties_pass(self) -> None:
        self.assertEqual(
            split_maven_args("-fae -Drevision=1-SNAPSHOT -Pci"),
            ["-fae", "-Drevision=1-SNAPSHOT", "-Pci"],
        )

    def test_quiet_flags_pass_but_are_dropped(self) -> None:
        # fetch reads Maven's INFO output, which -q would hide
        self.assertEqual(
            split_maven_args("-q -Pci --quiet --color -q -B"),
            ["-Pci", "--color", "-B"],
        )


class FakeMavenTestCase(TempTestCase):
    """Provides a stand-in mvn script that reports a given version."""

    def fake_mvn(self, script: str, banner: str = "Apache Maven 3.9.9 (abc)") -> str:
        path = self.tmp / "mvn"
        version = f'case " $* " in *" --version "*) echo \'{banner}\'; exit 0 ;; esac\n'
        _ = path.write_text(
            "#!/bin/sh\n" + version + textwrap.dedent(script), encoding="utf-8"
        )
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)


class TestDiscoverCoordinates(FakeMavenTestCase):
    def test_maven_before_3_9_is_refused(self) -> None:
        # Its launcher ignores MAVEN_ARGS, so a deploy on it would not
        # see the -Drevision fetch replays, and deploy unseeded
        os.environ["MAVEN_ARGS"] = "-Drevision=2-SNAPSHOT"
        self.addCleanup(os.environ.pop, "MAVEN_ARGS")
        for version, accepted in (
            ("2.2.1", False),
            ("3.6.3", False),
            ("3.8.8", False),
            ("3.9.0", True),
            ("3.10.0", True),
            ("4.0.0-rc-7", True),
        ):
            with self.subTest(version=version):
                mvn = self.fake_mvn(
                    print_listing("g:a:jar:1-SNAPSHOT"),
                    banner=f"Apache Maven {version} (abc)",
                )
                if accepted:
                    _ = discover_coordinates(
                        self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn
                    )
                    continue
                with self.assertRaisesRegex(ActionError, "needs Maven 3.9 or newer"):
                    _ = discover_coordinates(
                        self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn
                    )

    def test_an_unreadable_maven_version_fails_closed(self) -> None:
        mvn = self.fake_mvn("exit 0\n", banner="Maven home: /opt/maven")
        with self.assertRaisesRegex(ActionError, "no Maven version"):
            _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)

    def test_reads_the_listing_without_defining_any_property(self) -> None:
        # A property fetch defined would reach model interpolation and
        # profile activation, which the deploy would not share
        log = self.tmp / "args.log"
        mvn = self.fake_mvn(
            f"printf '%s\\n' \"$@\" > '{log}'\n" + print_listing("g.h:a:jar:1-SNAPSHOT")
        )
        coordinates = discover_coordinates(
            self.tmp, "pom.xml", "-Pci", "3.5.2", self.tmp, mvn
        )
        self.assertEqual([c.group_id for c in coordinates], ["g.h"])
        args = log.read_text(encoding="utf-8").split("\n")
        self.assertIn(f"{HELP_PLUGIN}:3.5.2:active-profiles", args)
        self.assertEqual([a for a in args if a.startswith("-D")], [])

    def test_quiet_flags_are_not_replayed(self) -> None:
        # -q hides the INFO listing fetch reads, and shapes no reactor
        log = self.tmp / "args.log"
        mvn = self.fake_mvn(
            f"printf '%s\\n' \"$@\" > '{log}'\n" + print_listing("g:a:jar:1-SNAPSHOT")
        )
        os.environ["MAVEN_ARGS"] = "-q -Pextra"
        self.addCleanup(os.environ.pop, "MAVEN_ARGS")
        _ = discover_coordinates(
            self.tmp, "pom.xml", "--quiet -Pci --color -q", "3.5.2", self.tmp, mvn
        )
        args = log.read_text(encoding="utf-8").split("\n")
        self.assertNotIn("-q", args)
        self.assertNotIn("--quiet", args)
        self.assertIn("-Pextra", args)
        self.assertIn("--color", args)

    def test_a_successful_run_listing_no_projects_fails(self) -> None:
        mvn = self.fake_mvn("echo '[INFO] Active profile report written to: x'\n")
        with self.assertRaisesRegex(ActionError, "listed no projects"):
            _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)

    def test_output_that_is_not_utf_8_is_still_read(self) -> None:
        mvn = self.fake_mvn(
            "printf '[INFO] \\377\\376\\n'\n" + print_listing("g:a:jar:1-SNAPSHOT")
        )
        coordinates = discover_coordinates(
            self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn
        )
        self.assertEqual([c.artifact_id for c in coordinates], ["a"])

    def test_maven_args_from_the_environment_are_checked_and_replayed(self) -> None:
        log = self.tmp / "args.log"
        mvn = self.fake_mvn(
            f"printf '%s\\n' \"$@\" > '{log}'\n"
            + f"env | grep -c '^MAVEN_ARGS=' >> '{log}' || true\n"
            + print_listing("g:a:jar:1-SNAPSHOT")
        )
        os.environ["MAVEN_ARGS"] = "-Pextra -Drevision=2-SNAPSHOT"
        self.addCleanup(os.environ.pop, "MAVEN_ARGS")
        _ = discover_coordinates(self.tmp, "pom.xml", "-Pci", "3.5.2", self.tmp, mvn)
        lines = log.read_text(encoding="utf-8").split("\n")
        # Replayed on the command line, ahead of maven_args, as Maven would
        self.assertLess(lines.index("-Pextra"), lines.index("-Pci"))
        self.assertIn("-Drevision=2-SNAPSHOT", lines)
        # and not left in the environment for Maven to read unchecked
        self.assertIn("0", lines)

    def test_unsafe_maven_args_in_the_environment_fail(self) -> None:
        mvn = self.fake_mvn("exit 0\n")
        os.environ["MAVEN_ARGS"] = "-pl core"
        self.addCleanup(os.environ.pop, "MAVEN_ARGS")
        with self.assertRaisesRegex(ActionError, "MAVEN_ARGS: .*narrows the reactor"):
            _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)

    def test_maven_args_the_launcher_would_expand_fail(self) -> None:
        # Maven's launcher word-splits and glob-expands MAVEN_ARGS, so a
        # replay of these tokens could differ from what the deploy reads
        mvn = self.fake_mvn("exit 0\n")
        self.addCleanup(os.environ.pop, "MAVEN_ARGS", None)
        for value in ("-Dinclude=*", "-Dx=a?", "-Dx=[ab]", "-Dx=a\u00a0-Pb"):
            with self.subTest(value=value):
                os.environ["MAVEN_ARGS"] = value
                with self.assertRaisesRegex(ActionError, "MAVEN_ARGS: .*launcher"):
                    _ = discover_coordinates(
                        self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn
                    )

    def test_maven_failure_is_an_action_error(self) -> None:
        mvn = self.fake_mvn("echo 'boom'; exit 3\n")
        with self.assertRaisesRegex(ActionError, "exit 3"):
            _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)

    def test_maven_does_not_inherit_the_nexus_password(self) -> None:
        env_log = self.tmp / "env.log"
        mvn = self.fake_mvn(
            f"env > '{env_log}'\n" + print_listing("g:a:jar:1-SNAPSHOT")
        )
        password = f"{ACTION_INPUT_PREFIX}NEXUS_PASSWORD"
        os.environ[password] = "s3cr3t-value"
        self.addCleanup(os.environ.pop, password)
        _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)
        seen = env_log.read_text(encoding="utf-8")
        self.assertNotIn("s3cr3t-value", seen)
        self.assertNotIn(ACTION_INPUT_PREFIX, seen)
        self.assertIn("PATH=", seen)

    def test_maven_environment_drops_action_inputs_and_maven_args(self) -> None:
        env = maven_environment(
            {
                f"{ACTION_INPUT_PREFIX}NEXUS_PASSWORD": "x",
                "MAVEN_ARGS": "-pl app",
                "JAVA_HOME": "/j",
            }
        )
        self.assertEqual(env, {"JAVA_HOME": "/j"})

    def test_a_callers_own_input_variables_reach_maven(self) -> None:
        # A profile may activate on env.INPUT_MODE, and the deploy that
        # follows still sees the caller's value, so fetch must too, even
        # where the name matches one of this action's inputs
        caller = {"INPUT_MODE": "extra", "INPUT_NEXUS_PASSWORD": "theirs"}
        env = maven_environment({**caller, f"{ACTION_INPUT_PREFIX}MODE": "fetch"})
        self.assertEqual(env, caller)

    def test_the_stripped_variables_match_action_yaml(self) -> None:
        # One variable per declared input, all set by the step's env
        text = (
            pathlib.Path(__file__).resolve().parent.parent / "action.yaml"
        ).read_text(encoding="utf-8")
        inputs_block = text.split("\noutputs:")[0]
        names: list[str] = re.findall(r"^  ([a-z0-9_]+):$", inputs_block, re.M)
        declared = {f"{ACTION_INPUT_PREFIX}{name.upper()}" for name in names}
        found: list[str] = re.findall(r"^\s+([A-Z][A-Z0-9_]*):", text, re.M)
        exported = set(found)
        self.assertEqual(declared, exported)
        self.assertEqual(set(ACTION_INPUT_VARIABLES), exported)

    def test_missing_maven_is_reported(self) -> None:
        with self.assertRaisesRegex(ActionError, "not found on PATH"):
            _ = discover_coordinates(
                self.tmp, "pom.xml", "", "3.5.2", self.tmp, str(self.tmp / "absent")
            )


class TestMavenConfigPom(FakeMavenTestCase):
    """A .mvn/maven.config may pick the POM a plain 'mvn deploy' builds.

    A command-line -f overrides it, so fetch's -f and a deploy without
    one can read different reactors. With pom_file unset, fetch cannot
    tell which the deploy does, so it requires both to agree.
    """

    log: Path = Path()

    @override
    def setUp(self) -> None:
        super().setUp()
        self.log = self.tmp / "calls.log"

    def branching_mvn(self, with_file: str, without_file: str | None) -> str:
        """Lists ``with_file`` given -f, else ``without_file`` or fails."""
        plain = print_listing(without_file) if without_file else "exit 1\n"
        return self.fake_mvn(
            f"printf '%s\\n' \"$*\" >> '{self.log}'\n"
            + 'case " $* " in *" -f "*)\n'
            + print_listing(with_file)
            + ";;\n*)\n"
            + plain
            + ";;\nesac\n"
        )

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()

    def discover(
        self, mvn: str, pom_file: str = "", project: Path | None = None
    ) -> list[Coordinate]:
        project = project or self.tmp
        return discover_coordinates(project, pom_file, "", "3.5.2", self.tmp, mvn)

    def test_without_a_maven_config_fetch_reads_pom_xml_once(self) -> None:
        mvn = self.branching_mvn("g:a:jar:1-SNAPSHOT", "g:other:jar:1-SNAPSHOT")
        self.assertEqual([c.artifact_id for c in self.discover(mvn)], ["a"])
        self.assertEqual(len(self.calls()), 1)
        self.assertIn(" -f pom.xml ", f" {self.calls()[0]} ")

    def test_a_config_selecting_another_reactor_fails(self) -> None:
        _ = self.write(self.tmp, ".mvn/maven.config", b"--file=alt.xml\n")
        mvn = self.branching_mvn("g:a:jar:1-SNAPSHOT", "g:alt:pom:1-SNAPSHOT")
        with self.assertRaisesRegex(ActionError, "maven.config.*set pom_file"):
            _ = self.discover(mvn)
        self.assertNotIn(" -f ", f" {self.calls()[1]} ")

    def test_a_config_agreeing_with_pom_xml_passes(self) -> None:
        _ = self.write(self.tmp, ".mvn/maven.config", b"-T\n4\n")
        mvn = self.branching_mvn("g:a:jar:1-SNAPSHOT", "g:a:jar:1-SNAPSHOT")
        self.assertEqual([c.artifact_id for c in self.discover(mvn)], ["a"])
        self.assertEqual(len(self.calls()), 2)

    def test_a_config_above_path_prefix_counts(self) -> None:
        # Maven walks up from the project to the first .mvn it finds
        _ = self.write(self.tmp, ".mvn/maven.config", b"--file=alt.xml\n")
        (self.tmp / "sub").mkdir()
        mvn = self.branching_mvn("g:a:jar:1-SNAPSHOT", "g:alt:pom:1-SNAPSHOT")
        with self.assertRaisesRegex(ActionError, "set pom_file"):
            _ = self.discover(mvn, project=self.tmp / "sub")

    def test_a_plain_run_that_fails_fails_closed(self) -> None:
        # e.g. the config names a POM that does not exist
        _ = self.write(self.tmp, ".mvn/maven.config", b"--file=missing.xml\n")
        mvn = self.branching_mvn("g:a:jar:1-SNAPSHOT", None)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ActionError, "without -f failed.*set pom_file"),
        ):
            _ = self.discover(mvn)

    def test_an_explicit_pom_file_is_passed_as_is(self) -> None:
        # The caller names the POM the deploy's own -f selects, which
        # overrides the config there as it does here
        _ = self.write(self.tmp, ".mvn/maven.config", b"--file=alt.xml\n")
        mvn = self.branching_mvn("g:a:jar:1-SNAPSHOT", "g:alt:pom:1-SNAPSHOT")
        self.assertEqual(
            [c.artifact_id for c in self.discover(mvn, pom_file="pom.xml")], ["a"]
        )
        self.assertEqual(len(self.calls()), 1)


@unittest.skipUnless(
    os.environ.get("RUN_MAVEN_TESTS") == "1",
    "runs Maven and resolves maven-help-plugin; set RUN_MAVEN_TESTS=1",
)
class TestRealMaven(TempTestCase):
    """Discovery on a real Maven, where the help plugin's own parameters bite.

    The reactor builds module c unless an 'output' property is defined,
    so a property fetch defined itself would hide a module the deploy
    builds. A help plugin 'artifact' parameter, from any source, would
    swap the whole reactor for one artifact under help:effective-pom.
    """

    BUILT: tuple[str, ...] = ("a", "b", "c", "root")

    def reactor(self, properties: str = "") -> Path:
        project = self.tmp / "project"
        # Marks the root for Maven 4, which warns without it
        (project / ".mvn").mkdir(parents=True, exist_ok=True)
        pom = f"""\
            <project xmlns="http://maven.apache.org/POM/4.0.0">
              <modelVersion>4.0.0</modelVersion>
              <groupId>org.fx</groupId><artifactId>root</artifactId>
              <version>1.0.0-SNAPSHOT</version><packaging>pom</packaging>
              <properties>{properties}</properties>
              <modules><module>a</module><module>b</module></modules>
              <profiles><profile><id>unless-output</id>
                <activation><property><name>!output</name></property></activation>
                <modules><module>c</module></modules>
              </profile></profiles>
            </project>
            """
        _ = self.write(project, "pom.xml", textwrap.dedent(pom).encode())
        for module in ("a", "b", "c"):
            child = f"""\
                <project xmlns="http://maven.apache.org/POM/4.0.0">
                  <modelVersion>4.0.0</modelVersion>
                  <parent><groupId>org.fx</groupId><artifactId>root</artifactId>
                    <version>1.0.0-SNAPSHOT</version></parent>
                  <artifactId>{module}</artifactId>
                </project>
                """
            _ = self.write(
                project, f"{module}/pom.xml", textwrap.dedent(child).encode()
            )
        return project

    def discover(
        self, project: Path, maven_args: str = "", pom_file: str = ""
    ) -> tuple[str, ...]:
        work = self.tmp / "work"
        work.mkdir(exist_ok=True)
        found = discover_coordinates(project, pom_file, maven_args, "3.5.2", work)
        return tuple(sorted(c.artifact_id for c in found))

    def with_maven_args(self, value: str) -> None:
        os.environ["MAVEN_ARGS"] = value
        self.addCleanup(os.environ.pop, "MAVEN_ARGS", None)

    def test_fetch_defines_no_property_the_build_would_not_see(self) -> None:
        self.assertEqual(self.discover(self.reactor()), self.BUILT)

    def test_an_artifact_property_cannot_narrow_the_reactor(self) -> None:
        artifact = "org.fx:a:1.0.0-SNAPSHOT"
        with self.subTest(source="POM property"):
            project = self.reactor(f"<artifact>{artifact}</artifact>")
            self.assertEqual(self.discover(project), self.BUILT)
        with self.subTest(source="maven_args"):
            project = self.reactor()
            self.assertEqual(
                self.discover(project, f"-Dartifact={artifact}"), self.BUILT
            )
        with self.subTest(source="MAVEN_ARGS"):
            self.with_maven_args(f"-Dartifact={artifact}")
            self.assertEqual(self.discover(self.reactor()), self.BUILT)

    def test_quiet_maven_args_still_read_the_reactor(self) -> None:
        self.with_maven_args("-q")
        self.assertEqual(self.discover(self.reactor(), "--quiet"), self.BUILT)

    def test_a_maven_config_selecting_another_pom_needs_pom_file(self) -> None:
        # A plain 'mvn deploy' builds alt.xml's reactor, while fetch's -f,
        # like maven-build-action's, would override the config
        project = self.reactor()
        alt = """\
            <project xmlns="http://maven.apache.org/POM/4.0.0">
              <modelVersion>4.0.0</modelVersion>
              <groupId>org.fx</groupId><artifactId>alt</artifactId>
              <version>1.0.0-SNAPSHOT</version><packaging>pom</packaging>
              <modules><module>a</module><module>b</module></modules>
            </project>
            """
        _ = self.write(project, "alt.xml", textwrap.dedent(alt).encode())
        _ = self.write(project, ".mvn/maven.config", b"--file=alt.xml\n")
        with self.assertRaisesRegex(ActionError, "set pom_file"):
            _ = self.discover(project)
        self.assertEqual(self.discover(project, pom_file="alt.xml"), ("a", "alt", "b"))
        self.assertEqual(self.discover(project, pom_file="pom.xml"), self.BUILT)

    def test_an_output_property_fails_closed(self) -> None:
        # It sends the listing to a file, leaving stdout without one
        project = self.reactor(f"<output>{self.tmp / 'report.txt'}</output>")
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(ActionError, "listed no projects"),
        ):
            _ = self.discover(project)


class TestRepositoryUrl(unittest.TestCase):
    def test_nexus2_and_nexus3_layouts(self) -> None:
        self.assertEqual(
            repository_base_url("https://nexus.example.org/", "snapshots", "2"),
            "https://nexus.example.org/content/repositories/snapshots/",
        )
        self.assertEqual(
            repository_base_url("https://nexus.example.org", "snapshots", "3"),
            "https://nexus.example.org/repository/snapshots/",
        )

    def test_unsafe_servers_are_rejected(self) -> None:
        for server in (
            "http://nexus.example.org",
            "https://user:pw@nexus.example.org",
            "https://nexus.example.org/?x=1",
            "ftp://nexus.example.org",
        ):
            with self.subTest(server=server), self.assertRaises(ActionError):
                _ = repository_base_url(server, "snapshots", "2")

    def test_bad_repository_and_version_are_rejected(self) -> None:
        with self.assertRaises(ActionError):
            _ = repository_base_url("https://n.example.org", "../x", "2")
        with self.assertRaises(ActionError):
            _ = repository_base_url("https://n.example.org", "snapshots", "4")

    def test_malformed_urls_are_action_errors(self) -> None:
        for server, message in (
            ("https://n.example.org:notaport", "not a valid URL"),
            ("https://n.example.org:99999", "not a valid URL"),
            ("https://[bad", "not a valid URL"),
            ("https://[::1", "not a valid URL"),
            ("https://a]b", "not a valid URL"),
            # IDNA would fail at connect time with a UnicodeError
            (f"https://{'a' * 64}.example.org", "invalid hostname"),
            ("https://nexus..example.org", "invalid hostname"),
        ):
            with (
                self.subTest(server=server),
                self.assertRaisesRegex(ActionError, message),
            ):
                _ = repository_base_url(server, "snapshots", "2")

    def test_non_ascii_context_paths_are_encoded(self) -> None:
        self.assertEqual(
            repository_base_url("https://n.example.org/naïve", "snapshots", "3"),
            "https://n.example.org/na%C3%AFve/repository/snapshots/",
        )
        # already encoded: left alone, not encoded twice
        self.assertIn(
            "/na%C3%AFve/",
            repository_base_url("https://n.example.org/na%C3%AFve", "snapshots", "3"),
        )

    def test_internationalised_hostnames_pass(self) -> None:
        url = repository_base_url("https://nexus.exämple.org", "snapshots", "2")
        self.assertIn("exämple", url)

    def test_dot_segment_repository_names_are_rejected(self) -> None:
        for name in (".", ".."):
            with self.subTest(name=name), self.assertRaises(ActionError):
                _ = repository_base_url("https://n.example.org", name, "3")

    def test_credentials_must_come_as_a_pair(self) -> None:
        self.assertIsNone(basic_auth("", ""))
        with self.assertRaises(ActionError):
            _ = basic_auth("user", "")


class TestFetch(NexusTestCase):
    def test_first_publish_seeds_nothing(self) -> None:
        result = self.seed([coordinate()])
        self.assertEqual(result.metadata, [])
        self.assertEqual(list(self.m2repo.rglob("*.xml")), [])
        self.assertTrue((self.baseline / BASELINE_MARKER).is_file())

    def test_published_metadata_is_seeded_into_m2repo_and_baseline(self) -> None:
        _ = self.write(self.server_root, f"{CORE_V}/maven-metadata.xml", metadata(41))
        _ = self.write(
            self.server_root,
            f"{CORE_V}/maven-metadata.xml.sha1",
            digest(metadata(41), "sha1"),
        )
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        result = self.seed([coordinate()])
        self.assertEqual(len(result.metadata), 2)
        self.assertEqual(result.files, 3)
        for root in (self.m2repo, self.baseline):
            self.assertEqual(
                (root / CORE_V / "maven-metadata.xml").read_bytes(), metadata(41)
            )
            self.assertTrue((root / CORE_V / "maven-metadata.xml.sha1").is_file())

    def test_a_checksum_not_matching_the_metadata_is_rejected(self) -> None:
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        # A stale digest: well formed, but of the metadata before it changed
        stale = ARTIFACT_METADATA.replace(
            b"<versioning/>", b"<versioning>x</versioning>"
        )
        cases = (
            (".sha1", digest(stale, "sha1")),
            (".md5", digest(stale, "md5")),
            (".sha1", digest(ARTIFACT_METADATA, "md5")),
            (".md5", b"not hex at all"),
        )
        for extension, body in cases:
            with self.subTest(extension=extension, body=body):
                for old in self.server_root.rglob("*.xml.*"):
                    old.unlink()
                _ = self.write(
                    self.server_root, f"{CORE}/maven-metadata.xml{extension}", body
                )
                with self.assertRaisesRegex(ActionError, "does not match"):
                    _ = self.seed([coordinate()])

    def test_checksum_files_may_carry_a_filename(self) -> None:
        # Some tools write "<hex>  <name>"; the value is the first word
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        _ = self.write(
            self.server_root,
            f"{CORE}/maven-metadata.xml.sha1",
            digest(ARTIFACT_METADATA, "sha1") + b"  maven-metadata.xml\n",
        )
        result = self.seed([coordinate()])
        self.assertEqual(result.files, 2)

    def test_md5_is_checked_where_fips_disables_it_for_security(self) -> None:
        # A FIPS OpenSSL build refuses MD5 unless the caller declares it
        # a non-security use, which Maven's legacy checksum is
        real_new = hashlib.new
        body = ARTIFACT_METADATA
        sidecar = digest(body, "md5")

        def fips_new(name: str, data: bytes = b"", **kwargs: bool) -> object:
            if name == "md5" and kwargs.get("usedforsecurity", True):
                raise ValueError("[digital envelope routines] unsupported")
            return real_new(name, data, **kwargs)

        with mock.patch("hashlib.new", fips_new):
            self.assertTrue(checksum_matches(sidecar, body, ".md5"))

    def test_partial_history_seeds_what_exists(self) -> None:
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        result = self.seed([coordinate()])
        self.assertEqual(result.metadata, [f"{CORE}/maven-metadata.xml"])

    def test_nexus3_layout_is_requested(self) -> None:
        _ = self.write(self.server_root, f"{CORE_V}/maven-metadata.xml", metadata(1))
        result = self.seed([coordinate()], base_url=self.base_url("3"))
        self.assertIn(f"{CORE_V}/maven-metadata.xml", result.metadata)
        self.assertTrue(
            all(r.path.startswith("/repository/") for r in self.mock.requests)
        )

    def test_authentication_failure_fails_closed(self) -> None:
        self.mock.expected_auth = basic_auth("user", "right")
        with self.assertRaisesRegex(ActionError, "HTTP 401"):
            _ = self.seed(
                [coordinate()], fetcher=self.fetcher(basic_auth("user", "wrong"))
            )

    def test_credentials_are_sent_only_when_configured(self) -> None:
        self.mock.expected_auth = basic_auth("user", "pw")
        _ = self.seed([coordinate()], fetcher=self.fetcher(basic_auth("user", "pw")))
        self.assertTrue(all(r.auth_ok for r in self.mock.requests))
        self.mock.expected_auth = None
        self.mock.requests.clear()
        _ = self.seed([coordinate()])
        self.assertFalse(any(r.auth for r in self.mock.requests))

    def test_transient_failures_retry_then_succeed(self) -> None:
        _ = self.write(self.server_root, f"{CORE_V}/maven-metadata.xml", metadata(7))
        self.mock.responses = {f"{CORE_V}/maven-metadata.xml": ["drop", 503, "ok"]}
        # "ok" is no scripted action, so the third request serves the file
        result = self.seed([coordinate()])
        self.assertIn(f"{CORE_V}/maven-metadata.xml", result.metadata)
        self.assertEqual(self.sleeps, (1, 2))

    def test_persistent_server_errors_fail_after_every_attempt(self) -> None:
        self.mock.responses = {f"{CORE}/maven-metadata.xml": [500]}
        with self.assertRaisesRegex(ActionError, "after 3 attempts: HTTP 500"):
            _ = self.seed([coordinate()])

    def test_other_client_errors_do_not_retry(self) -> None:
        self.mock.responses = {f"{CORE}/maven-metadata.xml": [400]}
        with self.assertRaisesRegex(ActionError, "HTTP 400"):
            _ = self.seed([coordinate()])
        self.assertEqual(self.sleeps, ())

    def test_redirects_are_refused_not_followed(self) -> None:
        self.mock.responses = {f"{CORE}/maven-metadata.xml": ["redirect"]}
        with self.assertRaisesRegex(ActionError, "redirect"):
            _ = self.seed([coordinate()])

    def test_non_metadata_body_is_rejected(self) -> None:
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", b"<html>login</html>"
        )
        with self.assertRaisesRegex(ActionError, "not XML metadata"):
            _ = self.seed([coordinate()])

    def test_an_xml_body_without_a_metadata_root_is_rejected(self) -> None:
        body = b'<?xml version="1.0"?><error>Unauthorized</error>'
        _ = self.write(self.server_root, f"{CORE}/maven-metadata.xml", body)
        with self.assertRaisesRegex(ActionError, "not XML metadata"):
            _ = self.seed([coordinate()])

    def test_a_declared_encoding_expat_cannot_use_is_rejected(self) -> None:
        # Unknown (LookupError), multi-byte (ValueError), and failing to
        # decode (UnicodeError): none of them a ParseError
        for encoding in ("x-unknown", "shift_jis", "idna"):
            body = f'<?xml version="1.0" encoding="{encoding}"?><metadata/>'
            _ = self.write(
                self.server_root, f"{CORE}/maven-metadata.xml", body.encode()
            )
            with (
                self.subTest(encoding=encoding),
                self.assertRaisesRegex(ActionError, "not XML metadata"),
            ):
                _ = self.seed([coordinate()])

    def test_interrupted_fetch_leaves_a_baseline_prune_refuses(self) -> None:
        # An earlier fetch completed, so its marker exists
        _ = self.seed([coordinate()])
        self.assertTrue((self.baseline / BASELINE_MARKER).is_file())
        # A rerun then fails partway through seeding
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        self.mock.responses = {f"{CORE_V}/maven-metadata.xml": [500]}
        with self.assertRaises(ActionError):
            _ = self.seed([coordinate()])
        with self.assertRaisesRegex(ActionError, "run mode 'fetch' first"):
            _ = prune_metadata(self.m2repo, self.baseline)

    def test_an_m2repo_already_holding_metadata_is_refused(self) -> None:
        # Left behind by an earlier run; the server has nothing for it,
        # so it would stay out of the baseline and publish unchanged
        _ = self.write(self.m2repo, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA)
        with self.assertRaisesRegex(ActionError, "needs a clean m2repo"):
            _ = self.seed([coordinate()])

    def test_a_dangling_metadata_symlink_in_the_m2repo_is_refused(self) -> None:
        # The server 404s, so seeding never writes through it; left in
        # place, the deploy would follow it out of the m2repo
        outside = self.tmp / "outside" / "maven-metadata.xml"
        (self.m2repo / CORE_V).mkdir(parents=True)
        (self.m2repo / CORE_V / "maven-metadata.xml").symlink_to(outside)
        with self.assertRaisesRegex(ActionError, "needs a clean m2repo"):
            _ = self.seed([coordinate()])
        self.assertFalse(outside.exists())
        self.assertFalse((self.baseline / BASELINE_MARKER).exists())

    def test_a_symlinked_directory_in_the_m2repo_is_refused(self) -> None:
        # The server 404s, so no path under it is ever written or checked;
        # left in place, the deploy would write through it
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.m2repo / "org").mkdir(parents=True)
        (self.m2repo / "org" / "example").symlink_to(outside)
        with self.assertRaisesRegex(ActionError, r"symbolic link.*org/example"):
            _ = self.seed([coordinate()])
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.baseline / BASELINE_MARKER).exists())

    @unittest.skipIf(os.geteuid() == 0, "root reads directories regardless of mode")
    def test_an_m2repo_directory_the_scan_cannot_read_fails(self) -> None:
        # Traversable but unreadable: a link inside would stay hidden
        (self.m2repo / "org").mkdir(parents=True)
        (self.m2repo / "org").chmod(0o300)
        self.addCleanup((self.m2repo / "org").chmod, 0o700)
        with self.assertRaises(PermissionError):
            _ = self.seed([coordinate()])
        self.assertFalse((self.baseline / BASELINE_MARKER).exists())

    def test_a_symlinked_m2repo_root_is_accepted(self) -> None:
        # A caller may keep the whole m2repo elsewhere; only links beneath
        # it could lead the deploy out of the tree that publishes
        _ = self.write(self.m2repo, f"{CORE_V}/core.jar", b"jar")
        alias = self.tmp / "alias"
        alias.symlink_to(self.m2repo)
        _ = seed_metadata(
            [coordinate()], self.base_url(), self.fetcher(), alias, self.baseline
        )
        self.assertTrue((self.baseline / BASELINE_MARKER).is_file())

    def test_an_m2repo_holding_artefacts_only_is_accepted(self) -> None:
        _ = self.write(self.m2repo, f"{CORE_V}/core.jar", b"jar")
        _ = self.seed([coordinate()])
        self.assertTrue((self.baseline / BASELINE_MARKER).is_file())

    def test_a_symlink_escaping_the_m2repo_is_refused(self) -> None:
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.m2repo / "org").mkdir(parents=True)
        (self.m2repo / "org" / "example").symlink_to(outside)
        with self.assertRaisesRegex(ActionError, "symbolic link at org/example"):
            _ = self.seed([coordinate()])
        self.assertEqual(list(outside.iterdir()), [])


class TestPrune(NexusTestCase):
    def fetch_core(self) -> None:
        _ = self.write(
            self.server_root, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        _ = self.write(self.server_root, f"{CORE_V}/maven-metadata.xml", metadata(41))
        _ = self.write(
            self.server_root,
            f"{CORE_V}/maven-metadata.xml.md5",
            digest(metadata(41), "md5"),
        )
        _ = self.seed([coordinate()])

    def redeploy_core(self) -> None:
        """What maven-deploy-plugin does: new metadata, new checksum."""
        _ = self.write(self.m2repo, f"{CORE_V}/maven-metadata.xml", metadata(42))
        _ = self.write(
            self.m2repo, f"{CORE_V}/maven-metadata.xml.md5", digest(metadata(42), "md5")
        )

    def test_redeployed_metadata_is_kept(self) -> None:
        self.fetch_core()
        # The build deployed core again, so its metadata moved on
        self.redeploy_core()
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(result.removed, [f"{CORE}/maven-metadata.xml"])
        self.assertEqual(
            (self.m2repo / CORE_V / "maven-metadata.xml").read_bytes(), metadata(42)
        )

    def test_a_stale_checksum_beside_redeployed_metadata_fails(self) -> None:
        self.fetch_core()
        # The XML moved on, but the seeded build-41 .md5 was never rewritten
        _ = self.write(self.m2repo, f"{CORE_V}/maven-metadata.xml", metadata(42))
        with self.assertRaisesRegex(ActionError, "stale checksum"):
            _ = prune_metadata(self.m2repo, self.baseline)

    def test_untouched_metadata_and_its_siblings_are_removed(self) -> None:
        self.fetch_core()
        # A signature has checksums of its own, which would otherwise
        # replace the server's for a signature this build never ships
        for suffix in (".asc", ".asc.md5", ".asc.sha1", ".asc.sha256", ".asc.sha512"):
            _ = self.write(self.m2repo, f"{CORE_V}/maven-metadata.xml{suffix}", b"x")
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(len(result.removed), 2)
        self.assertEqual(list(self.m2repo.rglob("maven-metadata.xml*")), [])
        self.assertEqual(result.removed_files, 8)
        self.assertEqual(result.kept, 0)

    def test_sibling_published_during_the_build_is_not_reverted(self) -> None:
        self.fetch_core()
        # A sibling build published build 43 while this one ran; this
        # build did not redeploy core, so its seeded copy must not ship
        _ = self.write(self.server_root, f"{CORE_V}/maven-metadata.xml", metadata(43))
        _ = prune_metadata(self.m2repo, self.baseline)
        self.assertFalse((self.m2repo / CORE_V / "maven-metadata.xml").exists())

    def test_checksums_of_metadata_the_build_removed_are_pruned(self) -> None:
        self.fetch_core()
        # The build deleted a seeded metadata file but left its checksum
        (self.m2repo / CORE_V / "maven-metadata.xml").unlink()
        self.assertTrue((self.m2repo / CORE_V / "maven-metadata.xml.md5").is_file())
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertIn(f"{CORE_V}/maven-metadata.xml", result.removed)
        self.assertFalse((self.m2repo / CORE_V / "maven-metadata.xml.md5").exists())

    def test_versions_of_one_artifact_share_its_artifact_level_file(self) -> None:
        # Why lanes must serialise per repository, not per branch: two
        # versions of one artifact both rewrite this same file
        master = Coordinate("org.example", "core", "1.1.0-SNAPSHOT", "jar")
        stable = Coordinate("org.example", "core", "1.0.1-SNAPSHOT", "jar")
        shared = set(master.metadata_paths()) & set(stable.metadata_paths())
        self.assertEqual(shared, {f"{CORE}/maven-metadata.xml"})

    def test_artefacts_are_never_touched(self) -> None:
        self.fetch_core()
        jar = self.write(
            self.m2repo, f"{CORE_V}/core-1.0.0-20260925.120000-42.jar", b"jar"
        )
        _ = prune_metadata(self.m2repo, self.baseline)
        self.assertTrue(jar.is_file())

    def test_prune_without_a_baseline_fails(self) -> None:
        with self.assertRaisesRegex(ActionError, "run mode 'fetch' first"):
            _ = prune_metadata(self.m2repo, self.tmp / "nothing")

    def test_a_symlink_the_build_left_is_not_followed(self) -> None:
        # fetch refuses links it finds, but the build runs after it
        self.fetch_core()
        outside = self.tmp / "outside"
        _ = shutil.move(str(self.m2repo / CORE), str(outside))
        (self.m2repo / CORE).symlink_to(outside)
        with self.assertRaisesRegex(ActionError, "resolves outside"):
            _ = prune_metadata(self.m2repo, self.baseline)
        self.assertTrue((outside / "maven-metadata.xml").is_file())

    @unittest.skipIf(sys.version_info >= (3, 13), "3.13 resolves a loop as is")
    def test_a_symlink_loop_the_build_left_is_an_action_error(self) -> None:
        self.fetch_core()
        shutil.rmtree(self.m2repo / CORE)
        (self.m2repo / CORE).symlink_to(self.m2repo / CORE)
        with self.assertRaisesRegex(ActionError, "symbolic link loop"):
            _ = prune_metadata(self.m2repo, self.baseline)

    def test_a_baseline_fetched_into_another_m2repo_is_refused(self) -> None:
        # Every baseline file would be missing there and count as pruned,
        # while the seeded copies in the real m2repo went on to publish
        self.fetch_core()
        seeded = sorted(self.m2repo.rglob("maven-metadata.xml*"))
        with self.assertRaisesRegex(ActionError, "same m2repo_path"):
            _ = prune_metadata(self.tmp / "elsewhere", self.baseline)
        self.assertEqual(sorted(self.m2repo.rglob("maven-metadata.xml*")), seeded)

    def test_the_m2repo_is_compared_once_resolved(self) -> None:
        self.fetch_core()
        alias = self.tmp / "alias"
        alias.symlink_to(self.m2repo)
        result = prune_metadata(alias, self.baseline)
        self.assertEqual(len(result.removed), 2)

    def deploy_extra(self) -> None:
        """What a POM-bound deploy-file writes: a coordinate fetch never saw."""
        _ = self.write(
            self.m2repo, f"{EXTRA_V}/extra-1.0.0-20260925.120000-1.jar", b"jar"
        )
        _ = self.write(self.m2repo, f"{EXTRA_V}/maven-metadata.xml", metadata(1))
        _ = self.write(
            self.m2repo, f"{GROUP}/extra/maven-metadata.xml", ARTIFACT_METADATA
        )

    @unittest.expectedFailure
    def test_fetch_records_the_reactor_it_read(self) -> None:
        _ = self.seed([coordinate("tool", "maven-plugin"), coordinate()])
        self.assertEqual(
            read_json(self.baseline / COORDINATES_RECORD),
            [record(), record("tool", "maven-plugin")],
        )

    @unittest.expectedFailure
    def test_an_extra_coordinate_inside_a_known_group_fails(self) -> None:
        # Its metadata was never seeded, so the deploy numbered it from 1;
        # published, it would roll back whatever Nexus serves for it
        self.fetch_core()
        self.redeploy_core()
        self.deploy_extra()
        before = sorted(self.m2repo.rglob("*"))
        with self.assertRaises(ActionError) as caught:
            _ = prune_metadata(self.m2repo, self.baseline)
        message = str(caught.exception)
        self.assertIn("org.example:extra:1.0.0-SNAPSHOT", message)
        self.assertIn(f"{GROUP}/extra/maven-metadata.xml", message)
        self.assertIn("buildNumber", message)
        self.assertNotIn("org.example:core", message)
        self.assertNotIn(f"{CORE}/", message)
        # Refused before prune removed anything
        self.assertEqual(sorted(self.m2repo.rglob("*")), before)

    def test_attached_artefacts_of_a_recorded_module_pass(self) -> None:
        self.fetch_core()
        self.redeploy_core()
        _ = self.write(self.m2repo, f"{CORE}/maven-metadata.xml", b"<metadata/>\n")
        stem = f"{CORE_V}/core-1.0.0-20260925.120000-42"
        for suffix in (
            ".jar",
            "-sources.jar",
            "-javadoc.jar",
            "-tests.jar",
            ".pom",
            ".module",
            ".jar.asc",
            ".jar.sha1",
            ".pom.md5",
        ):
            _ = self.write(self.m2repo, f"{stem}{suffix}", b"x")
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(result.kept, 2)

    def test_a_recorded_plugins_group_index_passes(self) -> None:
        _ = self.seed([coordinate("tool", "maven-plugin")])
        version = f"{GROUP}/tool/1.0.0-SNAPSHOT"
        _ = self.write(
            self.m2repo, f"{version}/tool-1.0.0-20260925.120000-1.jar", b"jar"
        )
        _ = self.write(self.m2repo, f"{version}/maven-metadata.xml", metadata(1))
        for index in (f"{GROUP}/tool", GROUP):
            _ = self.write(
                self.m2repo, f"{index}/maven-metadata.xml", ARTIFACT_METADATA
            )
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(result.kept, 3)

    @unittest.expectedFailure
    def test_a_group_index_no_recorded_plugin_owns_fails(self) -> None:
        self.fetch_core()
        _ = self.write(self.m2repo, f"{GROUP}/maven-metadata.xml", ARTIFACT_METADATA)
        with self.assertRaisesRegex(ActionError, f"{GROUP}/maven-metadata.xml"):
            _ = prune_metadata(self.m2repo, self.baseline)

    @unittest.expectedFailure
    def test_a_baseline_without_a_coordinates_record_fails(self) -> None:
        self.fetch_core()
        (self.baseline / COORDINATES_RECORD).unlink()
        with self.assertRaisesRegex(ActionError, "no record of the reactor"):
            _ = prune_metadata(self.m2repo, self.baseline)

    @unittest.expectedFailure
    def test_a_malformed_coordinates_record_fails(self) -> None:
        self.fetch_core()
        path = self.baseline / COORDINATES_RECORD
        traversal = {**record(), "artifactId": ".."}
        for content in (
            "not json",
            json.dumps(record()),
            json.dumps([["org.example", "core"]]),
            json.dumps([{"groupId": "org.example"}]),
            json.dumps([{**record(), "version": 1}]),
            json.dumps([{**record(), "classifier": "tests"}]),
            json.dumps([traversal]),
        ):
            _ = path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(
                ActionError, "not a valid coordinates record", msg=content
            ):
                _ = prune_metadata(self.m2repo, self.baseline)

    def test_recorded_names_ending_in_snapshot_pass(self) -> None:
        # Discovery accepts both, so only the record can tell a recorded
        # group or artifact directory from a version directory
        for module in (
            Coordinate("org.example", "core-SNAPSHOT", "1.0.0-SNAPSHOT", "jar"),
            Coordinate("org-SNAPSHOT", "core", "1.0.0-SNAPSHOT", "jar"),
        ):
            with self.subTest(module=module):
                shutil.rmtree(self.m2repo, ignore_errors=True)
                _ = self.seed([module])
                artifact = f"{module.group_path}/{module.artifact_id}"
                version = f"{artifact}/{module.version}"
                _ = self.write(
                    self.m2repo, f"{version}/x-1.0.0-20260925.120000-1.jar", b"jar"
                )
                _ = self.write(
                    self.m2repo, f"{version}/maven-metadata.xml", metadata(1)
                )
                _ = self.write(
                    self.m2repo, f"{artifact}/maven-metadata.xml", ARTIFACT_METADATA
                )
                result = prune_metadata(self.m2repo, self.baseline)
                self.assertEqual(result.kept, 2)

    @unittest.expectedFailure
    def test_a_version_directory_colliding_with_a_recorded_artifact_fails(
        self,
    ) -> None:
        # Maven's layout puts org:foo:bar-SNAPSHOT where org.foo:bar-SNAPSHOT
        # keeps its artifact-level metadata, so the directory's name alone
        # cannot clear it; the version files inside give it away
        _ = self.seed(
            [
                Coordinate("org", "foo", "1-SNAPSHOT", "jar"),
                Coordinate("org.foo", "bar-SNAPSHOT", "1-SNAPSHOT", "jar"),
            ]
        )
        shared = "org/foo/bar-SNAPSHOT"
        _ = self.write(self.m2repo, f"{shared}/maven-metadata.xml", ARTIFACT_METADATA)
        _ = self.write(self.m2repo, f"{shared}/maven-metadata.xml.sha1", b"x")
        _ = self.write(self.m2repo, f"{shared}/1-SNAPSHOT/bar-1-1.jar", b"jar")
        _ = prune_metadata(self.m2repo, self.baseline)
        _ = self.write(self.m2repo, f"{shared}/foo-bar-20260925.120000-1.jar", b"jar")
        with self.assertRaisesRegex(ActionError, "SNAPSHOT org:foo:bar-SNAPSHOT"):
            _ = prune_metadata(self.m2repo, self.baseline)

    @unittest.expectedFailure
    def test_a_release_sharing_recorded_metadata_fails(self) -> None:
        # One path, two roles: a recorded SNAPSHOT's version metadata, or
        # a recorded plugin's group index, is also the artifact metadata
        # of an unrecorded release beneath it, which its deploy rewrites
        for recorded, release, named in (
            (
                Coordinate("org", "foo", "bar-SNAPSHOT", "jar"),
                "org/foo/bar-SNAPSHOT/1.0",
                "version org.foo:bar-SNAPSHOT:1.0",
            ),
            (
                Coordinate("org.foo", "tool", "1-SNAPSHOT", "maven-plugin"),
                "org/foo/1.0",
                "version org:foo:1.0",
            ),
        ):
            with self.subTest(release=release):
                shutil.rmtree(self.m2repo, ignore_errors=True)
                _ = self.seed([recorded])
                shared = release.rsplit("/", 1)[0]
                _ = self.write(self.m2repo, f"{release}/x-1.0.jar", b"jar")
                _ = self.write(
                    self.m2repo, f"{shared}/maven-metadata.xml", ARTIFACT_METADATA
                )
                with self.assertRaisesRegex(ActionError, f"{re.escape(named)}[,.] "):
                    _ = prune_metadata(self.m2repo, self.baseline)

    def test_a_recorded_release_beneath_its_metadata_passes(self) -> None:
        _ = self.seed([Coordinate("org.example", "lib", "1.0.0", "jar")])
        _ = self.write(self.m2repo, f"{GROUP}/lib/1.0.0/lib-1.0.0.jar", b"jar")
        _ = self.write(
            self.m2repo, f"{GROUP}/lib/maven-metadata.xml", ARTIFACT_METADATA
        )
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(result.kept, 1)

    @unittest.expectedFailure
    def test_an_orphaned_metadata_sidecar_no_module_owns_fails(self) -> None:
        # Without its XML, a checksum or signature would still publish
        # over the server's own, so it is judged like the metadata
        self.fetch_core()
        orphan = f"{GROUP}/extra/maven-metadata.xml"
        for suffix in (".sha1", ".asc"):
            _ = self.write(self.m2repo, f"{orphan}{suffix}", b"x")
        with self.assertRaisesRegex(ActionError, f"metadata {orphan}\\.asc[,.] "):
            _ = prune_metadata(self.m2repo, self.baseline)

    @unittest.expectedFailure
    def test_unrecorded_names_ending_in_snapshot_name_their_coordinate(
        self,
    ) -> None:
        # The name alone does not make a version directory, so the scan
        # reaches the real version and names it, not a group or artifact
        self.fetch_core()
        for version in (
            "org/foo-SNAPSHOT/extra/1-SNAPSHOT",
            f"{GROUP}/extra-SNAPSHOT/1-SNAPSHOT",
        ):
            artifact = version.rsplit("/", 1)[0]
            _ = self.write(self.m2repo, f"{version}/x-1-20260925.120000-1.jar", b"jar")
            _ = self.write(self.m2repo, f"{version}/maven-metadata.xml", metadata(1))
            _ = self.write(
                self.m2repo, f"{artifact}/maven-metadata.xml", ARTIFACT_METADATA
            )
        with self.assertRaises(ActionError) as caught:
            _ = prune_metadata(self.m2repo, self.baseline)
        message = str(caught.exception)
        for named in (
            "SNAPSHOT org.foo-SNAPSHOT:extra:1-SNAPSHOT",
            "metadata org/foo-SNAPSHOT/extra/maven-metadata.xml",
            "SNAPSHOT org.example:extra-SNAPSHOT:1-SNAPSHOT",
            f"metadata {GROUP}/extra-SNAPSHOT/maven-metadata.xml",
        ):
            self.assertRegex(message, f"{re.escape(named)}[,.] ")
        self.assertNotRegex(message, r"SNAPSHOT org/foo-SNAPSHOT[,.] ")
        self.assertNotIn("SNAPSHOT org:example:extra-SNAPSHOT", message)

    def test_a_linked_recorded_container_is_not_entered(self) -> None:
        # It would hold a version's files only beyond the link, outside
        # the m2repo, where the scan must not look
        _ = self.seed([Coordinate("org.example", "core-SNAPSHOT", "1-SNAPSHOT", "jar")])
        outside = self.tmp / "outside"
        _ = self.write(outside, "core-SNAPSHOT-1-20260925.120000-1.jar", b"jar")
        (self.m2repo / GROUP).mkdir(parents=True)
        (self.m2repo / GROUP / "core-SNAPSHOT").symlink_to(outside)
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(result.removed, [])

    @unittest.expectedFailure
    def test_an_unrecorded_linked_snapshot_directory_is_reported(self) -> None:
        # Not entered, so its contents cannot clear it
        self.fetch_core()
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.m2repo / GROUP / "other-SNAPSHOT").symlink_to(outside)
        with self.assertRaisesRegex(ActionError, "SNAPSHOT org:example:other-SNAPSHOT"):
            _ = prune_metadata(self.m2repo, self.baseline)

    @unittest.expectedFailure
    def test_a_version_directory_too_shallow_for_a_coordinate_fails(self) -> None:
        # Named by its path, since it holds no group or no artifact
        self.fetch_core()
        _ = self.write(self.m2repo, "stray-SNAPSHOT/x.jar", b"jar")
        _ = self.write(self.m2repo, "org/x-SNAPSHOT/x.jar", b"jar")
        with self.assertRaises(ActionError) as caught:
            _ = prune_metadata(self.m2repo, self.baseline)
        message = str(caught.exception)
        self.assertRegex(message, r"SNAPSHOT stray-SNAPSHOT[,.] ")
        self.assertRegex(message, r"SNAPSHOT org/x-SNAPSHOT[,.] ")


class TestOutputHandling(TempTestCase):
    def test_error_text_cannot_start_a_workflow_command(self) -> None:
        escaped = escape_command_data("bad\n::add-mask::x 100%\r")
        self.assertNotIn("\n", escaped)
        self.assertNotIn("\r", escaped)
        self.assertEqual(escaped, "bad%0A::add-mask::x 100%25%0D")

    def test_emitted_lines_reach_a_pipe_while_the_process_runs(self) -> None:
        # Piped stdout is block-buffered: without a flush the line would
        # sit in the buffer until exit, and this read would time out
        script = (
            "import sys, time; sys.path.insert(0, 'src');"
            "from snapshot_metadata.workflow import emit;"
            "emit('::notice::early'); time.sleep(30)"
        )
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        assert proc.stdout is not None
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        self.assertTrue(ready, "line not flushed while the process ran")
        self.assertEqual(proc.stdout.readline(), b"::notice::early\n")

    def test_retry_notices_cannot_start_a_workflow_command(self) -> None:
        # BadStatusLine keeps a server's raw line, line breaks and all
        def hostile(url: str, auth: str | None, timeout: float) -> Response:
            del url, auth, timeout
            raise http.client.BadStatusLine("HTTP/1.1 200\n::set-output name=x::y")

        fetcher = Fetcher(None, 2, 0, get=hostile, sleep=lambda _: None)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(ActionError):
                _ = fetcher.fetch("https://n.example.org/x")
        for line in out.getvalue().splitlines():
            self.assertFalse(line.startswith("::set-output"), line)

    def test_masks_register_the_secret_itself(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mask("p%25w\nsecond")
        self.assertEqual(
            out.getvalue().splitlines(),
            ["::add-mask::p%2525w", "::add-mask::second"],
        )

    def test_outputs_use_a_delimiter_absent_from_the_value(self) -> None:
        output = self.tmp / "output"
        os.environ["GITHUB_OUTPUT"] = str(output)
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT")
        write_outputs({"group_paths": "org/example\nforged=1"})
        text = output.read_text(encoding="utf-8")
        delimiter = text.split("<<", 1)[1].split("\n", 1)[0]
        self.assertTrue(text.endswith(f"\n{delimiter}\n"))
        self.assertNotIn(delimiter, "org/example\nforged=1")


class TestEntryPoint(FakeMavenTestCase):
    """cli.main() turns bad input into an annotation, never a traceback."""

    def run_main(self, **inputs: str) -> tuple[int, str]:
        env = {
            "GITHUB_WORKSPACE": str(self.tmp),
            "RUNNER_TEMP": str(self.tmp / "runner-temp"),
            f"{ACTION_INPUT_PREFIX}NEXUS_SERVER": "https://nexus.example.org",
            f"{ACTION_INPUT_PREFIX}REPOSITORY_NAME": "snapshots",
        }
        env.update({f"{ACTION_INPUT_PREFIX}{k.upper()}": v for k, v in inputs.items()})
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = cli.main()
        finally:
            for key, value in saved.items():
                if value is None:
                    _ = os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return code, out.getvalue()

    def assert_clean_error(self, expected: str, **inputs: str) -> None:
        code, out = self.run_main(**inputs)
        self.assertEqual(code, 1)
        self.assertIn("::error::", out)
        self.assertIn(expected, out)

    def complete_fetch(self) -> Path:
        """A baseline as a successful fetch of nothing leaves it."""
        baseline = self.tmp / "runner-temp" / "maven-snapshot-metadata" / "baseline"
        fetcher = Fetcher(None, 1, 0)
        _ = seed_metadata([], "unused", fetcher, self.tmp / "m2repo", baseline)
        return baseline

    def test_a_failed_fetch_leaves_no_baseline_prune_accepts(self) -> None:
        # An always() prune after a fetch that failed early must not take
        # an earlier fetch's baseline in the same job as current
        for failing in (
            {"fetch_attempts": "0"},
            {"nexus_server": "ftp://nexus.example.org"},
            {"path_prefix": "absent"},
        ):
            with self.subTest(**failing):
                _ = self.complete_fetch()
                self.assertEqual(self.run_main(mode="prune")[0], 0)
                self.assertEqual(self.run_main(mode="fetch", **failing)[0], 1)
                self.assert_clean_error("run mode 'fetch' first", mode="prune")

    def test_a_missing_path_prefix_is_an_error(self) -> None:
        self.assert_clean_error(
            "is not a directory", mode="fetch", path_prefix="absent"
        )

    def test_a_file_as_path_prefix_is_an_error(self) -> None:
        _ = (self.tmp / "regular-file").write_text("x", encoding="utf-8")
        self.assert_clean_error(
            "is not a directory", mode="fetch", path_prefix="regular-file"
        )

    def test_a_missing_pom_is_an_error(self) -> None:
        (self.tmp / "project").mkdir()
        self.assert_clean_error("does not exist", mode="fetch", path_prefix="project")

    def test_a_symlink_loop_in_an_input_path_is_an_error(self) -> None:
        # Python 3.12 and older raise RuntimeError resolving one, which
        # main() does not catch; 3.13 resolves it and fails further on
        (self.tmp / "loop").symlink_to(self.tmp / "loop")
        for inputs in (
            {"mode": "fetch", "path_prefix": "loop"},
            {"mode": "fetch", "pom_file": "loop"},
            {"mode": "prune", "m2repo_path": "loop"},
        ):
            with self.subTest(**inputs):
                self.assert_clean_error("::error::", **inputs)

    def test_an_unknown_mode_is_an_error(self) -> None:
        self.assert_clean_error("mode must be", mode="publish")

    @unittest.expectedFailure
    def test_check_coordinates_takes_true_or_false(self) -> None:
        _ = self.complete_fetch()
        self.assert_clean_error(
            "check_coordinates must be", mode="prune", check_coordinates="yes"
        )

    @unittest.expectedFailure
    def test_prune_refuses_a_coordinate_fetch_did_not_record(self) -> None:
        nexus = MockNexus(root=self.tmp / "server").start()
        self.addCleanup(nexus.stop)
        _ = self.fake_mvn(print_listing("org.example:core:jar:1.0.0-SNAPSHOT"))
        _ = self.write(self.tmp, "pom.xml", b"<project/>\n")
        output = self.tmp / "github-output"
        path = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", path)
        os.environ["PATH"] = f"{self.tmp}{os.pathsep}{path}"
        os.environ["GITHUB_OUTPUT"] = str(output)
        self.addCleanup(os.environ.pop, "GITHUB_OUTPUT")
        code, out = self.run_main(mode="fetch", nexus_server=nexus.url)
        self.assertEqual(code, 0, out)
        recorded = Path(read_outputs(output)["coordinates_path"])
        self.assertEqual(read_json(recorded), [record()])
        # The deploy wrote a module the reactor never listed
        m2repo = self.tmp / "m2repo"
        _ = self.write(m2repo, f"{CORE_V}/core-1.0.0-20260925.120000-1.jar", b"jar")
        _ = self.write(m2repo, f"{EXTRA_V}/extra-1.0.0-20260925.120000-1.jar", b"jar")
        self.assert_clean_error("org.example:extra:1.0.0-SNAPSHOT", mode="prune")
        code, out = self.run_main(mode="prune", check_coordinates="false")
        self.assertEqual(code, 0, out)

    @unittest.skipIf(os.geteuid() == 0, "root reads files regardless of mode")
    def test_os_errors_become_annotations(self) -> None:
        # A seeded metadata file prune cannot read: a real PermissionError
        baseline = self.complete_fetch()
        seeded = self.write(baseline, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA)
        _ = self.write(
            self.tmp / "m2repo", f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        seeded.chmod(0)
        self.addCleanup(seeded.chmod, 0o600)
        # fetch recorded no module, so the coordinate check would refuse
        # core before prune reached the file; this test is about the file
        self.assert_clean_error(
            "PermissionError", mode="prune", check_coordinates="false"
        )


if __name__ == "__main__":
    _ = unittest.main()
