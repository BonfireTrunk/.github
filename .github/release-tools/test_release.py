"""Focused unit tests for the embedded release-workflow helpers."""
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import textwrap
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


TOOLS_DIRECTORY = Path(__file__).parent


def load_release_tools():
    """Load the same concatenated helpers that render.py embeds in workflows."""
    module = types.ModuleType("release_tools_under_test")
    module.__file__ = str(TOOLS_DIRECTORY / "common.py")
    for filename in ("common.py", "prepare.py", "bundle.py", "publish.py"):
        source = TOOLS_DIRECTORY / filename
        exec(compile(source.read_text(), source, "exec"), module.__dict__)
    return module


def load_renderer():
    source = TOOLS_DIRECTORY / "render.py"
    module = types.ModuleType("release_workflow_renderer_under_test")
    module.__file__ = str(source)
    exec(compile(source.read_text(), source, "exec"), module.__dict__)
    return module


TOOLS = load_release_tools()
RENDER = load_renderer()


class PythonIsolationTest(unittest.TestCase):
    def test_rendered_helper_ignores_checkout_modules_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trusted = root / "trusted"
            checkout = root / "checkout"
            trusted.mkdir()
            checkout.mkdir()
            (trusted / "common.py").write_text((TOOLS_DIRECTORY / "common.py").read_text())
            (trusted / "probe.py").write_text('print("trusted imports only")\n')
            for name in ("json.py", "sitecustomize.py"):
                (checkout / name).write_text('raise RuntimeError("CHECKOUT_MODULE_EXECUTED")\n')
            with patch.object(RENDER, "HERE", trusted):
                step = RENDER.step_script("Probe", "probe.py")
            script = textwrap.dedent(step.split("        run: |\n", 1)[1])
            result = TOOLS.subprocess.run(
                ["bash", "-e"], input=script, cwd=checkout, text=True,
                capture_output=True, timeout=10,
                env={"PATH": os.environ["PATH"], "PYTHONPATH": str(checkout)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(result.stdout, "trusted imports only\n")


class VersionTest(unittest.TestCase):
    def test_normalizes_title_shorthand_and_keeps_prereleases(self):
        self.assertEqual(TOOLS.version("1.2", shorthand=True), "1.2.0")
        self.assertEqual(TOOLS.version("1.2.3-hotfix.1"), "1.2.3-hotfix.1")

    def test_rejects_non_semver_and_leading_zero_prerelease_numbers(self):
        for value in ("01.2.3", "1.02.3", "1.2.03", "1.2", "1.2.3.4",
                      "1.2.3-patch/hotfix", "1.2.3+build", "1.2.3-rc.01"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    TOOLS.version(value)

    def test_parses_squash_merge_release_title(self):
        self.assertEqual(TOOLS.release_title("Release 1.2 (#42)\n\nMerged body"), "1.2.0")
        with self.assertRaisesRegex(ValueError, "Expected a squash-merge"):
            TOOLS.release_title("release 1.2.0")


class VersionKeyTest(unittest.TestCase):
    def test_orders_numeric_prereleases_before_stable_release(self):
        alpha_two = TOOLS.version_key("1.2.3-alpha.2")
        alpha_ten = TOOLS.version_key("1.2.3-alpha.10")
        stable = TOOLS.version_key("1.2.3")
        self.assertLess(alpha_two, alpha_ten)
        self.assertLess(alpha_ten, stable)


class NpmRepositoryTest(unittest.TestCase):
    def test_accepts_canonical_https_repository_forms(self):
        expected = "https://github.com/BonfireTrunk/example"
        packages = [
            {"repository": expected},
            {"repository": expected + ".git"},
            {"repository": "git+" + expected + ".git"},
            {"repository": {"type": "git", "url": expected}},
        ]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "BonfireTrunk/example"}, clear=True):
            for package in packages:
                with self.subTest(repository=package["repository"]):
                    TOOLS.check_npm_repository(package)

    def test_rejects_missing_or_mismatched_repository(self):
        packages = [
            {},
            {"repository": "https://github.com/BonfireTrunk/another"},
            {"repository": {"url": "https://github.com/BonfireTrunk/example", "directory": "packages/example"}},
        ]
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "BonfireTrunk/example"}, clear=True):
            for package in packages:
                with self.subTest(package=package):
                    with self.assertRaisesRegex(ValueError, "repository|root npm package"):
                        TOOLS.check_npm_repository(package)


class ChangelogTest(unittest.TestCase):
    def test_returns_only_the_exact_nonempty_release_section(self):
        notes = TOOLS.changelog_notes(
            "# Changelog\n\n## [1.2.3] - 2026-11-05\n\n- Fix retry\n\n## [1.2.2] - 2026-10-01\n\n- Previous\n",
            "1.2.3",
        )
        self.assertEqual(notes, "- Fix retry\n")

    def test_rejects_missing_or_empty_exact_section(self):
        for changelog in (
            "## [1.2.3]\n\n- Missing date\n",
            "## [1.2.3] - 2026-11-05\n\n",
            "## [1.2.30] - 2026-11-05\n\n- Different version\n",
        ):
            with self.subTest(changelog=changelog):
                with self.assertRaisesRegex(ValueError, "Expected one nonempty CHANGELOG"):
                    TOOLS.changelog_notes(changelog, "1.2.3")


class TagTest(unittest.TestCase):
    def test_resolves_an_annotated_tag_to_its_commit(self):
        github = Mock(side_effect=[
            {"object": {"type": "tag", "sha": "tag-object"}},
            {"object": {"type": "commit", "sha": "release-commit"}},
        ])
        with patch.object(TOOLS, "github", github):
            self.assertEqual(TOOLS.tag_commit("v1.2.3"), "release-commit")
        self.assertEqual(github.call_args_list[0].args, ("/git/ref/tags/v1.2.3",))
        self.assertEqual(github.call_args_list[1].args, ("/git/tags/tag-object",))

    def test_prepare_rejects_a_tag_on_another_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            event = temporary_path / "event.json"
            event.write_text(json.dumps({"repository": {"default_branch": "trunk", "private": False}}))
            environment = {
                "KIND": "go",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REF": "refs/heads/trunk",
                "GITHUB_SHA": "release-commit",
                "RUNNER_TEMP": temporary,
            }
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(TOOLS, "run", side_effect=["release-commit", "Release 1.2.3", "Release 1.2.3"]), \
                    patch.object(TOOLS, "github", return_value={"merge_base_commit": {"sha": "release-commit"}}), \
                    patch.object(TOOLS, "tag_commit", return_value="other-commit"):
                with self.assertRaisesRegex(ValueError, "Version tag already points at another commit"):
                    TOOLS.prepare()


class PrepareTest(unittest.TestCase):
    def test_rejects_blank_release_environment_before_reading_event_or_running_commands(self):
        with patch.dict(os.environ, {"KIND": "go", "RELEASE_ENVIRONMENT": "  "}, clear=True), \
                patch.object(TOOLS, "run") as run:
            with self.assertRaisesRegex(ValueError, "release-environment must name a GitHub environment"):
                TOOLS.prepare()
        run.assert_not_called()

    def test_rejects_private_repository_before_running_release_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps({"repository": {"default_branch": "trunk", "private": True}}))
            environment = {
                "KIND": "go",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REF": "refs/heads/trunk",
                "GITHUB_SHA": "release-commit",
                "RUNNER_TEMP": temporary,
            }
            with patch.dict(os.environ, environment, clear=True), patch.object(TOOLS, "run") as run:
                with self.assertRaisesRegex(ValueError, "require a public repository"):
                    TOOLS.prepare()
            run.assert_not_called()

    def test_rejects_release_commit_outside_default_branch_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            event = Path(temporary) / "event.json"
            event.write_text(json.dumps({"repository": {"default_branch": "trunk", "private": False}}))
            environment = {
                "KIND": "go",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_EVENT_PATH": str(event),
                "GITHUB_REF": "refs/heads/trunk",
                "GITHUB_SHA": "release-commit",
                "RUNNER_TEMP": temporary,
            }
            with patch.dict(os.environ, environment, clear=True), \
                    patch.object(TOOLS, "run", side_effect=["release-commit", "Release 1.2.3", "Release 1.2.3"]), \
                    patch.object(TOOLS, "github", return_value={"merge_base_commit": {"sha": "another-commit"}}) as github, \
                    patch.object(TOOLS, "tag_commit") as tag_commit:
                with self.assertRaisesRegex(ValueError, "current default branch history"):
                    TOOLS.prepare()
            self.assertEqual(github.call_args.args, ("/compare/release-commit...trunk",))
            tag_commit.assert_not_called()


class BundleTest(unittest.TestCase):
    def make_npm_bundle(self, directory):
        notes = directory / "notes.md"
        package = directory / "package.tgz"
        notes.write_text("- Release\n")
        package.write_bytes(b"npm package bytes")
        metadata = {
            "kind": "npm",
            "sha": "release-commit",
            "version": "1.2.3",
            "tag": "v1.2.3",
            "files": {"notes.md": TOOLS.digest(notes), "package.tgz": TOOLS.digest(package)},
        }
        (directory / "metadata.json").write_text(json.dumps(metadata))
        return metadata

    def test_accepts_complete_bundle_with_matching_metadata_and_digests(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.make_npm_bundle(directory)
            self.assertEqual(TOOLS.check_bundle(directory, "npm", "release-commit", "1.2.3"), metadata)

    def test_rejects_other_commit_and_wrong_artifact_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.make_npm_bundle(directory)
            with self.assertRaisesRegex(ValueError, "Artifact belongs to another release"):
                TOOLS.check_bundle(directory, "npm", "another-commit", "1.2.3")
            metadata["files"]["package.tgz"] = "0" * 64
            (directory / "metadata.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "checksum mismatch: package.tgz"):
                TOOLS.check_bundle(directory, "npm", "release-commit", "1.2.3")

    def test_rejects_bundle_missing_its_required_package_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            metadata = self.make_npm_bundle(directory)
            metadata["files"].pop("package.tgz")
            (directory / "metadata.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "Incomplete release bundle"):
                TOOLS.check_bundle(directory, "npm", "release-commit", "1.2.3")


class CratePayloadTest(unittest.TestCase):
    def write_crate(self, directory, manifest):
        archive = directory / "example-crate-1.2.3.crate"
        content = io.BytesIO()
        with TOOLS.tarfile.open(fileobj=content, mode="w:gz") as tar:
            entry = TOOLS.tarfile.TarInfo("example-crate-1.2.3/Cargo.toml")
            entry.size = len(manifest)
            tar.addfile(entry, io.BytesIO(manifest))
        archive.write_bytes(content.getvalue())
        return archive

    def test_encodes_target_renamed_dependencies_and_exact_archive_bytes(self):
        manifest = b"""
[package]
name = "example-crate"
version = "1.2.3"
description = "fixture"

[dependencies]
json = { package = "serde_json", version = "1.0", features = ["raw_value"] }

[target.'cfg(unix)'.build-dependencies]
codegen = { package = "build-support", version = "0.4", default-features = false }
"""
        with tempfile.TemporaryDirectory() as temporary:
            archive = self.write_crate(Path(temporary), manifest)

            metadata, payload = TOOLS.crate_payload(archive)

            renamed = next(dep for dep in metadata["deps"] if dep["explicit_name_in_toml"] == "json")
            self.assertEqual(renamed["name"], "serde_json")
            self.assertEqual(renamed["features"], ["raw_value"])
            target = next(dep for dep in metadata["deps"] if dep["explicit_name_in_toml"] == "codegen")
            self.assertEqual(target["name"], "build-support")
            self.assertEqual(target["kind"], "build")
            self.assertEqual(target["target"], "cfg(unix)")
            self.assertFalse(target["default_features"])

            metadata_size = struct.unpack("<I", payload[:4])[0]
            self.assertEqual(json.loads(payload[4:4 + metadata_size]), metadata)
            archive_size = struct.unpack("<I", payload[4 + metadata_size:8 + metadata_size])[0]
            self.assertEqual(archive_size, len(archive.read_bytes()))
            self.assertEqual(payload[8 + metadata_size:], archive.read_bytes())

    def test_rejects_dependency_fields_that_crates_io_cannot_represent(self):
        template = b"""
[package]
name = "example-crate"
version = "1.2.3"

[dependencies]
dependency = { version = "1.0", %s }
"""
        for field, value in (("artifact", '"bin"'), ("target", '"x86_64-unknown-linux-gnu"'), ("lib", "true")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                archive = self.write_crate(Path(temporary), template % (field.encode() + b" = " + value.encode()))
                with self.assertRaisesRegex(ValueError, "artifact dependencies are not supported"):
                    TOOLS.crate_payload(archive)


class RegistryResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self):
        return self.body


class RenderTest(unittest.TestCase):
    def test_publish_download_uses_build_artifact_id_without_recomputing_the_name(self):
        for kind in ("go", "npm", "rust"):
            with self.subTest(kind=kind):
                workflow = RENDER.render(kind)
                self.assertIn("artifact-id: ${{ steps.archive.outputs.artifact-id }}", workflow)
                self.assertIn("artifact-ids: ${{ needs.build.outputs.artifact-id }}", workflow)
                self.assertIn(f"name: release-{kind}-${{{{ github.run_id }}}}-${{{{ github.run_attempt }}}}", workflow)
                self.assertEqual(workflow.count("github.run_attempt"), 1)


class PublishTest(unittest.TestCase):
    def npm_metadata(self):
        return {"name": "@bonfiretrunk/example", "version": "1.2.3"}

    def test_npm_publish_skips_when_exact_registry_integrity_exists(self):
        with patch.object(TOOLS, "registry_matches", return_value=True), \
                patch.object(TOOLS.subprocess, "run") as run:
            TOOLS.npm_publish(self.npm_metadata(), "https://registry.npmjs.org")
        run.assert_not_called()

    def test_npm_registry_rejects_a_conflicting_integrity(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            Path("package.tgz").write_bytes(b"expected npm archive")
            response = {"name": "@bonfiretrunk/example", "version": "1.2.3",
                        "dist": {"integrity": "sha512-different"}}
            with patch.object(TOOLS, "request", return_value=response):
                with self.assertRaisesRegex(ValueError, "different bytes or no SHA-512 integrity"):
                    TOOLS.registry_matches(self.npm_metadata(), "https://registry.npmjs.org")

    def test_npm_publish_rejects_newer_existing_channel_before_subprocess(self):
        cases = (
            ({"name": "@bonfiretrunk/example", "version": "1.2.3"}, "latest", "1.2.4"),
            ({"name": "@bonfiretrunk/example", "version": "1.2.3-rc.1"}, "next", "1.2.3-rc.2"),
        )
        for metadata, channel, existing in cases:
            with self.subTest(channel=channel, existing=existing), \
                    patch.object(TOOLS, "registry_matches", return_value=False), \
                    patch.object(TOOLS, "request", return_value={"version": existing}) as request, \
                    patch.object(TOOLS.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "move npm " + channel + " backwards"):
                    TOOLS.npm_publish(metadata, "https://registry.npmjs.org")
                self.assertIn("/" + channel, request.call_args.args[0])
                run.assert_not_called()

    def test_cargo_registry_accepts_exact_checksum_and_rejects_conflict_or_yank(self):
        metadata = {"name": "example-crate", "version": "1.2.3"}
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            Path("package.crate").write_bytes(b"crate bytes")
            checksum = TOOLS.digest("package.crate")
            exact = json.dumps({"vers": "1.2.3", "cksum": checksum}).encode() + b"\n"
            with patch.object(TOOLS.urllib.request, "urlopen", return_value=RegistryResponse(exact)):
                self.assertTrue(TOOLS.cargo_matches(metadata))
            conflict = json.dumps({"vers": "1.2.3", "cksum": "0" * 64}).encode() + b"\n"
            with patch.object(TOOLS.urllib.request, "urlopen", return_value=RegistryResponse(conflict)):
                with self.assertRaisesRegex(ValueError, "different bytes"):
                    TOOLS.cargo_matches(metadata)
            yanked = json.dumps({"vers": "1.2.3", "cksum": checksum, "yanked": True}).encode() + b"\n"
            with patch.object(TOOLS.urllib.request, "urlopen", return_value=RegistryResponse(yanked)):
                with self.assertRaisesRegex(ValueError, "was yanked"):
                    TOOLS.cargo_matches(metadata)

    def test_go_proxy_info_escapes_uppercase_path_and_requires_matching_origin(self):
        metadata = {"name": "github.com/BonfireTrunk/Example", "tag": "v1.2.3", "sha": "release-commit"}
        response = {"Version": "v1.2.3", "Origin": {"Hash": "release-commit"}}
        with patch.object(TOOLS, "request", return_value=response) as request:
            self.assertEqual(TOOLS.go_proxy_info(metadata), response)
        request.assert_called_once_with(
            "https://proxy.golang.org/github.com/!bonfire!trunk/!example/@v/v1.2.3.info",
            missing=True,
        )

    def test_go_proxy_info_allows_missing_but_rejects_another_version_or_origin(self):
        metadata = {"name": "github.com/BonfireTrunk/Example", "tag": "v1.2.3", "sha": "release-commit"}
        with patch.object(TOOLS, "request", return_value=None):
            self.assertIsNone(TOOLS.go_proxy_info(metadata))
        for response, message in (
            ({"Version": "v1.2.4", "Origin": {"Hash": "release-commit"}}, "another version"),
            ({"Version": "v1.2.3", "Origin": {"Hash": "other-commit"}}, "different or unverifiable source commit"),
            ({"Version": "v1.2.3", "Origin": {}}, "different or unverifiable source commit"),
        ):
            with self.subTest(response=response), patch.object(TOOLS, "request", return_value=response):
                with self.assertRaisesRegex(ValueError, message):
                    TOOLS.go_proxy_info(metadata)

    def test_ensure_github_release_repairs_missing_release_for_existing_tag(self):
        metadata = {"tag": "v1.2.3", "sha": "release-commit", "version": "1.2.3"}
        with tempfile.TemporaryDirectory() as temporary:
            archive = Path(temporary) / "release-npm.zip"
            notes = Path(temporary) / "notes.md"
            archive.write_bytes(b"release bundle")
            notes.write_text("- Release\n")
            environment = {"GITHUB_REPOSITORY": "BonfireTrunk/example"}
            release = {"draft": False, "prerelease": False, "assets": []}
            github = Mock(side_effect=[None, release])
            with patch.dict(os.environ, environment, clear=True), contextlib.chdir(temporary), \
                    patch.object(TOOLS, "tag_commit", return_value="release-commit"), \
                    patch.object(TOOLS, "github", github), \
                    patch.object(TOOLS.subprocess, "run") as run:
                TOOLS.ensure_github_release(metadata, archive)
            self.assertEqual(github.call_args_list[0].args, ("/releases/tags/v1.2.3",))
            self.assertEqual(github.call_args_list[1].args, ("/releases",))
            self.assertEqual(github.call_args_list[1].kwargs["method"], "POST")
            self.assertEqual(github.call_args_list[1].kwargs["data"]["target_commitish"], "release-commit")
            run.assert_called_once_with(
                ["gh", "release", "upload", "v1.2.3", str(archive), "--repo", "BonfireTrunk/example"],
                check=True,
            )


if __name__ == "__main__":
    unittest.main()
