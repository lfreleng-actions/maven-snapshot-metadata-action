# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Tests for the src/snapshot_metadata package, against a mock Nexus."""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import io
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

from snapshot_metadata import ActionError, cli
from snapshot_metadata.coordinates import (
    ACTION_INPUT_VARIABLES,
    HELP_PLUGIN,
    Coordinate,
    discover_coordinates,
    long_option_names,
    maven_environment,
    parse_effective_pom,
    split_maven_args,
    top_level_group_paths,
)
from snapshot_metadata.nexus import (
    Fetcher,
    Response,
    basic_auth,
    repository_base_url,
)
from snapshot_metadata.repository import (
    BASELINE_MARKER,
    FetchResult,
    prune_metadata,
    seed_metadata,
)
from snapshot_metadata.workflow import escape_command_data, mask, write_outputs
from tests.compat import override
from tests.mock_nexus import MockNexus

GROUP = "org/example"
CORE = f"{GROUP}/core"
CORE_V = f"{CORE}/1.0.0-SNAPSHOT"


def digest(body: bytes, algorithm: str) -> bytes:
    """A checksum file holding the true digest of ``body``."""
    return hashlib.new(algorithm, body).hexdigest().encode()


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


class TestEffectivePom(TempTestCase):
    def parse(self, body: str) -> list[Coordinate]:
        path = self.tmp / "effective-pom.xml"
        _ = path.write_text(textwrap.dedent(body), encoding="utf-8")
        return parse_effective_pom(path)

    def test_reactor_with_inherited_coordinates_and_default_packaging(self) -> None:
        coordinates = self.parse(
            """\
            <?xml version="1.0" encoding="UTF-8"?>
            <projects>
              <project xmlns="http://maven.apache.org/POM/4.0.0">
                <groupId>org.example</groupId><artifactId>parent</artifactId>
                <version>1.0.0-SNAPSHOT</version><packaging>pom</packaging>
              </project>
              <project xmlns="http://maven.apache.org/POM/4.0.0">
                <parent><groupId>org.other</groupId></parent>
                <groupId>org.example</groupId><artifactId>core</artifactId>
                <version>1.0.0-SNAPSHOT</version>
              </project>
            </projects>
            """
        )
        self.assertEqual(
            coordinates,
            [
                Coordinate("org.example", "core", "1.0.0-SNAPSHOT", "jar"),
                Coordinate("org.example", "parent", "1.0.0-SNAPSHOT", "pom"),
            ],
        )

    def test_single_module_project_root(self) -> None:
        coordinates = self.parse(
            """\
            <project xmlns="http://maven.apache.org/POM/4.0.0">
              <groupId>org.example</groupId><artifactId>solo</artifactId>
              <version>2.0-SNAPSHOT</version>
            </project>
            """
        )
        self.assertEqual([c.artifact_id for c in coordinates], ["solo"])

    def test_unresolved_property_is_rejected_with_a_hint(self) -> None:
        with self.assertRaisesRegex(ActionError, "unresolved property"):
            _ = self.parse(
                """\
                <project><groupId>org.example</groupId><artifactId>a</artifactId>
                <version>${revision}</version></project>
                """
            )

    def test_path_traversal_in_a_coordinate_is_rejected(self) -> None:
        with self.assertRaisesRegex(ActionError, "not a valid coordinate"):
            _ = self.parse(
                """\
                <project><groupId>org.example</groupId><artifactId>..</artifactId>
                <version>1-SNAPSHOT</version></project>
                """
            )

    def test_malformed_document_is_an_action_error(self) -> None:
        with self.assertRaisesRegex(ActionError, "cannot parse"):
            _ = self.parse("<projects><project>")


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


class TestDiscoverCoordinates(TempTestCase):
    def fake_mvn(self, script: str) -> str:
        path = self.tmp / "mvn"
        _ = path.write_text("#!/bin/sh\n" + textwrap.dedent(script), encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_writes_output_after_caller_args_and_parses_it(self) -> None:
        log = self.tmp / "args.log"
        mvn = self.fake_mvn(
            f"""\
            printf '%s\\n' "$@" > '{log}'
            for arg in "$@"; do
              case "$arg" in -Doutput=*) out="${{arg#-Doutput=}}" ;; esac
            done
            printf '<project><groupId>g.h</groupId><artifactId>a</artifactId>'\\
            '<version>1-SNAPSHOT</version></project>' > "$out"
            """
        )
        coordinates = discover_coordinates(
            self.tmp, "pom.xml", "-Doutput=/elsewhere -Pci", "3.5.2", self.tmp, mvn
        )
        self.assertEqual([c.group_id for c in coordinates], ["g.h"])
        args = log.read_text(encoding="utf-8").split("\n")
        self.assertIn(f"{HELP_PLUGIN}:3.5.2:effective-pom", args)
        outputs = [a for a in args if a.startswith("-Doutput=")]
        self.assertEqual(outputs[-1], f"-Doutput={self.tmp / 'effective-pom.xml'}")

    def test_maven_args_from_the_environment_are_checked_and_replayed(self) -> None:
        log = self.tmp / "args.log"
        mvn = self.fake_mvn(
            f"""\
            printf '%s\\n' "$@" > '{log}'
            env | grep -c '^MAVEN_ARGS=' >> '{log}' || true
            for arg in "$@"; do
              case "$arg" in -Doutput=*) out="${{arg#-Doutput=}}" ;; esac
            done
            printf '<project><groupId>g</groupId><artifactId>a</artifactId>'\\
            '<version>1-SNAPSHOT</version></project>' > "$out"
            """
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

    def test_maven_failure_is_an_action_error(self) -> None:
        mvn = self.fake_mvn("echo 'boom'; exit 3\n")
        with self.assertRaisesRegex(ActionError, "exit 3"):
            _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)

    def test_maven_does_not_inherit_the_nexus_password(self) -> None:
        env_log = self.tmp / "env.log"
        mvn = self.fake_mvn(
            f"""\
            env > '{env_log}'
            for arg in "$@"; do
              case "$arg" in -Doutput=*) out="${{arg#-Doutput=}}" ;; esac
            done
            printf '<project><groupId>g</groupId><artifactId>a</artifactId>'\\
            '<version>1-SNAPSHOT</version></project>' > "$out"
            """
        )
        os.environ["INPUT_NEXUS_PASSWORD"] = "s3cr3t-value"
        self.addCleanup(os.environ.pop, "INPUT_NEXUS_PASSWORD")
        _ = discover_coordinates(self.tmp, "pom.xml", "", "3.5.2", self.tmp, mvn)
        seen = env_log.read_text(encoding="utf-8")
        self.assertNotIn("s3cr3t-value", seen)
        self.assertNotIn("INPUT_", seen)
        self.assertIn("PATH=", seen)

    def test_maven_environment_drops_action_inputs_and_maven_args(self) -> None:
        env = maven_environment(
            {"INPUT_NEXUS_PASSWORD": "x", "MAVEN_ARGS": "-pl app", "JAVA_HOME": "/j"}
        )
        self.assertEqual(env, {"JAVA_HOME": "/j"})

    def test_a_callers_own_input_variables_reach_maven(self) -> None:
        # A profile may activate on env.INPUT_INCLUDE_EXTRA, and the deploy
        # that follows still sees it, so fetch must too
        env = maven_environment({"INPUT_INCLUDE_EXTRA": "1", "INPUT_MODE": "fetch"})
        self.assertEqual(env, {"INPUT_INCLUDE_EXTRA": "1"})

    def test_the_stripped_variables_match_action_yaml(self) -> None:
        # One INPUT_<NAME> per declared input, all set by the step's env
        text = (
            pathlib.Path(__file__).resolve().parent.parent / "action.yaml"
        ).read_text(encoding="utf-8")
        inputs_block = text.split("\noutputs:")[0]
        names: list[str] = re.findall(r"^  ([a-z0-9_]+):$", inputs_block, re.M)
        declared = {f"INPUT_{name.upper()}" for name in names}
        found: list[str] = re.findall(r"^\s+(INPUT_[A-Z0-9_]+):", text, re.M)
        exported = set(found)
        self.assertEqual(declared, exported)
        self.assertEqual(set(ACTION_INPUT_VARIABLES), exported)

    def test_missing_maven_is_reported(self) -> None:
        with self.assertRaisesRegex(ActionError, "not found on PATH"):
            _ = discover_coordinates(
                self.tmp, "pom.xml", "", "3.5.2", self.tmp, str(self.tmp / "absent")
            )


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
        with self.assertRaisesRegex(ActionError, "outside"):
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
        _ = self.write(self.m2repo, f"{CORE_V}/maven-metadata.xml.asc", b"sig")
        result = prune_metadata(self.m2repo, self.baseline)
        self.assertEqual(len(result.removed), 2)
        self.assertEqual(list(self.m2repo.rglob("maven-metadata.xml*")), [])
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


class TestEntryPoint(TempTestCase):
    """cli.main() turns bad input into an annotation, never a traceback."""

    def run_main(self, **inputs: str) -> tuple[int, str]:
        env = {
            "GITHUB_WORKSPACE": str(self.tmp),
            "RUNNER_TEMP": str(self.tmp / "runner-temp"),
            "INPUT_NEXUS_SERVER": "https://nexus.example.org",
            "INPUT_REPOSITORY_NAME": "snapshots",
        }
        env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()})
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

    def test_an_unknown_mode_is_an_error(self) -> None:
        self.assert_clean_error("mode must be", mode="publish")

    @unittest.skipIf(os.geteuid() == 0, "root reads files regardless of mode")
    def test_os_errors_become_annotations(self) -> None:
        # A seeded metadata file prune cannot read: a real PermissionError
        baseline = self.tmp / "runner-temp" / "maven-snapshot-metadata" / "baseline"
        baseline.mkdir(parents=True)
        _ = (baseline / BASELINE_MARKER).write_text("fetch\n", encoding="utf-8")
        seeded = self.write(baseline, f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA)
        _ = self.write(
            self.tmp / "m2repo", f"{CORE}/maven-metadata.xml", ARTIFACT_METADATA
        )
        seeded.chmod(0)
        self.addCleanup(seeded.chmod, 0o600)
        self.assert_clean_error("PermissionError", mode="prune")


if __name__ == "__main__":
    _ = unittest.main()
