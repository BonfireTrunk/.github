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


class ReleaseAssetsTest(unittest.TestCase):
    def test_returns_digests_of_flat_regular_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "tool_linux_amd64.tar.gz").write_bytes(b"binary")
            (directory / "checksums.txt").write_bytes(b"sums")
            self.assertEqual(TOOLS.asset_files(directory, "go"), {
                "checksums.txt": TOOLS.hashlib.sha256(b"sums").hexdigest(),
                "tool_linux_amd64.tar.gz": TOOLS.hashlib.sha256(b"binary").hexdigest(),
            })

    def test_rejects_an_empty_directory_subdirectories_and_symlinks(self):
        for setup, message in (
            (lambda d: None, "left no files"),
            (lambda d: (d / "nested").mkdir(), "regular files directly"),
            (lambda d: ((d / "target").write_bytes(b"x"), (d / "link").symlink_to(d / "target")), "regular files directly"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                setup(Path(temporary))
                with self.assertRaisesRegex(ValueError, message):
                    TOOLS.asset_files(temporary, "go")

    def test_rejects_names_github_would_change_or_that_collide(self):
        for names in ([".hidden"], ["two words.txt"], ["trailing."], ["plus+sign"], ["release-go.zip"], ["Release-Go.zip"], ["path/name"]):
            with self.subTest(names=names):
                with self.assertRaisesRegex(ValueError, "Release asset names use"):
                    TOOLS.check_asset_names(names, "go")
        with self.assertRaisesRegex(ValueError, "differ in more than letter case"):
            TOOLS.check_asset_names(["Tool.tar.gz", "tool.tar.gz"], "go")
        TOOLS.check_asset_names(["release-npm.zip", "tool_1.2.3_linux_arm64.tar.gz"], "go")

    def test_check_bundle_rejects_an_invalid_sealed_asset_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "notes.md").write_text("- Release\n")
            metadata = {"kind": "go", "sha": "release-commit", "version": "1.2.3", "tag": "v1.2.3",
                        "files": {"notes.md": TOOLS.digest(directory / "notes.md")}, "assets": {"../escape": "0" * 64}}
            (directory / "metadata.json").write_text(json.dumps(metadata))
            with self.assertRaisesRegex(ValueError, "Release asset names use"):
                TOOLS.check_bundle(directory, "go", "release-commit", "1.2.3")

    def test_bundle_seals_asset_digests_but_not_their_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            (temp / "release-bundle").mkdir()
            (temp / "release-assets").mkdir()
            (temp / "release-assets" / "tool.tar.gz").write_bytes(b"binary")
            (temp / "release-bundle" / "notes.md").write_text("- Release\n")
            TOOLS.write_json(temp / "release-bundle" / "metadata.json", {
                "kind": "go", "sha": "release-commit", "version": "1.2.3", "tag": "v1.2.3",
                "name": "github.com/BonfireTrunk/example"})
            with patch.dict(os.environ, {"RUNNER_TEMP": temporary, "RELEASE_ASSETS": "true"}, clear=True):
                TOOLS.bundle()
            metadata = json.loads((temp / "release-bundle" / "metadata.json").read_text())
            self.assertEqual(metadata["assets"], {"tool.tar.gz": TOOLS.hashlib.sha256(b"binary").hexdigest()})
            with TOOLS.zipfile.ZipFile(temp / "release-go.zip") as zipped:
                self.assertEqual(sorted(zipped.namelist()), ["metadata.json", "notes.md"])

    def test_bundle_ignores_a_stray_asset_directory_unless_assets_were_requested(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            (temp / "release-bundle").mkdir()
            (temp / "release-assets" / "nested").mkdir(parents=True)
            (temp / "release-bundle" / "notes.md").write_text("- Release\n")
            TOOLS.write_json(temp / "release-bundle" / "metadata.json", {
                "kind": "go", "sha": "release-commit", "version": "1.2.3", "tag": "v1.2.3",
                "name": "github.com/BonfireTrunk/example"})
            for flag in (None, "false"):
                with self.subTest(flag=flag), patch.dict(os.environ, {"RUNNER_TEMP": temporary, **({"RELEASE_ASSETS": flag} if flag else {})}, clear=True):
                    TOOLS.bundle()
                    self.assertNotIn("assets", json.loads((temp / "release-bundle" / "metadata.json").read_text()))
                    (temp / "release-go.zip").unlink()

    def test_publish_uses_incoming_files_only_when_they_match_sealed_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "release-assets"
            sealed = {"tool.tar.gz": TOOLS.hashlib.sha256(b"binary").hexdigest()}
            self.assertEqual(TOOLS.incoming_assets(directory, {"assets": sealed}, "go"), {})
            directory.mkdir()
            (directory / "tool.tar.gz").write_bytes(b"binary")
            self.assertEqual(TOOLS.incoming_assets(directory, {"assets": sealed}, "go"), {"tool.tar.gz": directory / "tool.tar.gz"})
            self.assertEqual(TOOLS.incoming_assets(directory, {}, "go"), {})
            (directory / "tool.tar.gz").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "differ from the sealed bundle"):
                TOOLS.incoming_assets(directory, {"assets": sealed}, "go")
            (directory / "tool.tar.gz").write_bytes(b"binary")
            (directory / "extra.txt").write_bytes(b"extra")
            with self.assertRaisesRegex(ValueError, "differ from the sealed bundle"):
                TOOLS.incoming_assets(directory, {"assets": sealed}, "go")


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

    def test_only_go_builds_assets_and_passes_the_command_through_the_environment(self):
        workflow = RENDER.render("go")
        self.assertEqual(workflow.count("${{ inputs.assets-command }}"), 1)
        self.assertIn("          ASSETS_COMMAND: ${{ inputs.assets-command }}\n", workflow)
        self.assertIn('          bash -eo pipefail -c "$ASSETS_COMMAND"\n', workflow)
        self.assertLess(workflow.index("go vet ./..."), workflow.index("name: Build release assets"))
        self.assertLess(workflow.index("name: Build release assets"), workflow.index("name: Seal the release bundle"))
        self.assertIn("          path: |\n            ${{ runner.temp }}/release-go.zip\n"
                      "            ${{ runner.temp }}/release-assets\n", workflow)
        for kind in ("npm", "rust"):
            with self.subTest(kind=kind):
                self.assertNotIn("assets-command:", RENDER.render(kind))
                self.assertNotIn("inputs.assets-command", RENDER.render(kind))
                self.assertNotIn("${{ runner.temp }}/release-assets", RENDER.render(kind))


class PublishTest(unittest.TestCase):
    def npm_metadata(self):
        return {"name": "@bonfiretrunk/example", "version": "1.2.3"}

    def test_npm_publish_skips_when_exact_registry_integrity_exists(self):
        with patch.object(TOOLS, "registry_matches", return_value=True), \
                patch.object(TOOLS.subprocess, "run") as run:
            TOOLS.npm_publish(self.npm_metadata(), "https://registry.npmjs.org")
        run.assert_not_called()

    def packument(self, integrity, tags=None):
        return {"name": "@bonfiretrunk/example", "dist-tags": tags or {},
                "versions": {"1.2.3": {"name": "@bonfiretrunk/example", "version": "1.2.3",
                                       "dist": {"integrity": integrity}}}}

    def test_npm_registry_reads_the_package_document_that_github_packages_serves(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            Path("package.tgz").write_bytes(b"expected npm archive")
            integrity = "sha512-" + TOOLS.base64.b64encode(TOOLS.hashlib.sha512(b"expected npm archive").digest()).decode()
            with patch.object(TOOLS, "request", return_value=self.packument(integrity)) as request:
                self.assertTrue(TOOLS.registry_matches(self.npm_metadata(), "https://npm.pkg.github.com", "token"))
            request.assert_called_once_with("https://npm.pkg.github.com/%40bonfiretrunk%2Fexample", token="token", missing=True)
            for document in (None, {"name": "@bonfiretrunk/example", "versions": {}}):
                with self.subTest(document=document), patch.object(TOOLS, "request", return_value=document):
                    self.assertFalse(TOOLS.registry_matches(self.npm_metadata(), "https://npm.pkg.github.com", "token"))

    def test_npm_registry_rejects_a_conflicting_integrity_or_another_package(self):
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary):
            Path("package.tgz").write_bytes(b"expected npm archive")
            with patch.object(TOOLS, "request", return_value=self.packument("sha512-different")):
                with self.assertRaisesRegex(ValueError, "different bytes or no SHA-512 integrity"):
                    TOOLS.registry_matches(self.npm_metadata(), "https://registry.npmjs.org")
            with patch.object(TOOLS, "request", return_value={"name": "@bonfiretrunk/other", "versions": {}}):
                with self.assertRaisesRegex(ValueError, "another package identity"):
                    TOOLS.registry_matches(self.npm_metadata(), "https://registry.npmjs.org")

    def test_npm_publish_rejects_newer_existing_channel_before_subprocess(self):
        cases = (
            ({"name": "@bonfiretrunk/example", "version": "1.2.3"}, "latest", "1.2.4"),
            ({"name": "@bonfiretrunk/example", "version": "1.2.3-rc.1"}, "next", "1.2.3-rc.2"),
        )
        for metadata, channel, existing in cases:
            with self.subTest(channel=channel, existing=existing), \
                    patch.object(TOOLS, "registry_matches", return_value=False), \
                    patch.object(TOOLS, "request", return_value={"name": metadata["name"], "dist-tags": {channel: existing}}) as request, \
                    patch.object(TOOLS.subprocess, "run") as run:
                with self.assertRaisesRegex(ValueError, "move npm " + channel + " backwards"):
                    TOOLS.npm_publish(metadata, "https://registry.npmjs.org")
                self.assertEqual(request.call_args.args[0], "https://registry.npmjs.org/%40bonfiretrunk%2Fexample")
                run.assert_not_called()

    def test_npm_publish_waits_briefly_after_a_rejected_publish(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {"RUNNER_TEMP": temporary}, clear=True), \
                patch.object(TOOLS, "registry_matches", return_value=False) as matches, \
                patch.object(TOOLS, "check_npm_channel"), \
                patch.object(TOOLS.time, "sleep"), \
                patch.object(TOOLS.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "npm publish exit 1"):
                TOOLS.npm_publish(self.npm_metadata(), "https://registry.npmjs.org")
        self.assertEqual(matches.call_count, 1 + 3)

    def test_cargo_publish_reports_a_rejection_after_a_short_index_check(self):
        metadata = {"name": "example-crate", "version": "1.2.3"}
        rejection = RuntimeError("PUT crates.io returned HTTP 400: missing license; check access and service status")
        rejection.status = 400
        with tempfile.TemporaryDirectory() as temporary, contextlib.chdir(temporary), \
                patch.dict(os.environ, {"CARGO_REGISTRY_TOKEN": "token"}, clear=True), \
                patch.object(TOOLS, "cargo_matches", return_value=False) as matches, \
                patch.object(TOOLS, "request", side_effect=rejection), \
                patch.object(TOOLS.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            crate = b"crate bytes"
            info = json.dumps({"name": "example-crate", "vers": "1.2.3"}).encode()
            Path("package.crate").write_bytes(crate)
            Path("upload.bin").write_bytes(struct.pack("<I", len(info)) + info + struct.pack("<I", len(crate)) + crate)
            with self.assertRaisesRegex(RuntimeError, "missing license"):
                TOOLS.cargo_publish(metadata)
        self.assertEqual(matches.call_count, 1 + 2)

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

    def test_go_proxy_known_reads_only_the_version_list(self):
        metadata = {"name": "github.com/BonfireTrunk/Example", "tag": "v1.2.3", "sha": "release-commit"}
        for listing, known in ((b"v1.2.2\nv1.2.3\n", True), (b"v1.2.30\n", False), (b"", False), (None, False)):
            with self.subTest(listing=listing), patch.object(TOOLS, "request", return_value=listing) as request:
                self.assertEqual(TOOLS.go_proxy_known(metadata), known)
            request.assert_called_once_with(
                "https://proxy.golang.org/github.com/!bonfire!trunk/!example/@v/list", missing=True, raw=True)

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

    def release_fixture(self, temporary):
        archive = Path(temporary) / "release-npm.zip"
        archive.write_bytes(b"release bundle")
        Path(temporary, "notes.md").write_text("- Release\n")
        return {"tag": "v1.2.3", "sha": "release-commit", "version": "1.2.3"}, archive

    def run_ensure_release(self, temporary, archive, metadata, github, uploaded=None):
        environment = {"GITHUB_REPOSITORY": "BonfireTrunk/example", "GH_TOKEN": "token"}
        with patch.dict(os.environ, environment, clear=True), contextlib.chdir(temporary), \
                patch.object(TOOLS, "tag_commit", return_value="release-commit"), \
                patch.object(TOOLS, "github", github), \
                patch.object(TOOLS, "request", return_value=uploaded or {"digest": "sha256:" + TOOLS.digest(archive)}) as request:
            TOOLS.ensure_github_release(metadata, archive)
        return request

    def draft(self, assets=()):
        return {"id": 7, "draft": True, "prerelease": False, "tag_name": "v1.2.3", "assets": list(assets),
                "upload_url": "https://uploads.github.com/repos/BonfireTrunk/example/releases/7/assets{?name,label}"}

    def test_ensure_github_release_attaches_the_bundle_to_a_draft_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            github = Mock(side_effect=[None, [], self.draft(), None])
            request = self.run_ensure_release(temporary, archive, metadata, github)
            self.assertEqual(github.call_args_list[0].args, ("/releases/tags/v1.2.3",))
            self.assertEqual(github.call_args_list[2].args, ("/releases",))
            self.assertTrue(github.call_args_list[2].kwargs["data"]["draft"])
            self.assertEqual(github.call_args_list[2].kwargs["data"]["target_commitish"], "release-commit")
            self.assertEqual(request.call_args.args[0],
                             "https://uploads.github.com/repos/BonfireTrunk/example/releases/7/assets?name=release-npm.zip")
            self.assertEqual(request.call_args.kwargs["data"], b"release bundle")
            self.assertEqual(github.call_args_list[3].args, ("/releases/7",))
            self.assertEqual(github.call_args_list[3].kwargs["data"], {"draft": False, "make_latest": "legacy"})

    def test_ensure_github_release_replaces_a_draft_bundle_left_by_an_interrupted_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            stale = {"id": 11, "name": "release-npm.zip", "digest": "sha256:" + "0" * 64}
            github = Mock(side_effect=[None, [self.draft([stale])], None, None])
            request = self.run_ensure_release(temporary, archive, metadata, github)
            self.assertEqual(github.call_args_list[2].args, ("/releases/assets/11",))
            self.assertEqual(github.call_args_list[2].kwargs["method"], "DELETE")
            request.assert_called_once()
            self.assertEqual(github.call_args_list[3].kwargs["data"]["draft"], False)

    def test_ensure_github_release_never_replaces_a_published_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            published = dict(self.draft([{"id": 11, "name": "release-npm.zip", "digest": "sha256:" + "0" * 64}]), draft=False)
            github = Mock(side_effect=[published])
            with self.assertRaisesRegex(ValueError, "refusing to replace it"):
                self.run_ensure_release(temporary, archive, metadata, github)
            github.assert_called_once()

    def asset_fixture(self, temporary, metadata, files):
        directory = Path(temporary) / "release-assets"
        directory.mkdir()
        for name, content in files.items():
            (directory / name).write_bytes(content)
        metadata["assets"] = {name: TOOLS.hashlib.sha256(content).hexdigest() for name, content in files.items()}
        return {name: directory / name for name in files}

    def run_ensure_release_with_assets(self, temporary, archive, metadata, github, assets):
        def uploaded(url, **kwargs):
            return {"name": TOOLS.urllib.parse.unquote(url.split("?name=", 1)[1]),
                    "digest": "sha256:" + TOOLS.hashlib.sha256(kwargs["data"]).hexdigest()}
        environment = {"GITHUB_REPOSITORY": "BonfireTrunk/example", "GH_TOKEN": "token"}
        with patch.dict(os.environ, environment, clear=True), contextlib.chdir(temporary), \
                patch.object(TOOLS, "tag_commit", return_value="release-commit"), \
                patch.object(TOOLS, "github", github), \
                patch.object(TOOLS, "request", side_effect=uploaded) as request:
            TOOLS.ensure_github_release(metadata, archive, assets)
        return request

    def test_ensure_github_release_attaches_sealed_assets_to_the_draft_before_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            assets = self.asset_fixture(temporary, metadata, {"tool_linux_amd64.tar.gz": b"binary", "checksums.txt": b"sums"})
            github = Mock(side_effect=[None, [], self.draft(), None])
            request = self.run_ensure_release_with_assets(temporary, archive, metadata, github, assets)
            uploads = [(call.args[0].split("?name=", 1)[1], call.kwargs["content_type"]) for call in request.call_args_list]
            self.assertEqual(uploads, [("release-npm.zip", "application/zip"),
                                       ("tool_linux_amd64.tar.gz", "application/octet-stream"),
                                       ("checksums.txt", "application/octet-stream")])
            self.assertEqual(github.call_args_list[3].kwargs["data"]["draft"], False)

    def test_ensure_github_release_replaces_changed_and_removes_unsealed_draft_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            assets = self.asset_fixture(temporary, metadata, {"tool.tar.gz": b"rebuilt", "checksums.txt": b"sums"})
            bundle = {"id": 10, "name": "release-npm.zip", "digest": "sha256:" + TOOLS.digest(archive)}
            changed = {"id": 11, "name": "tool.tar.gz", "digest": "sha256:" + "0" * 64}
            same = {"id": 12, "name": "checksums.txt", "digest": "sha256:" + metadata["assets"]["checksums.txt"]}
            leftover = {"id": 13, "name": "old-name.tar.gz", "digest": "sha256:" + "1" * 64}
            github = Mock(side_effect=[None, [self.draft([bundle, changed, same, leftover])], None, None, None])
            request = self.run_ensure_release_with_assets(temporary, archive, metadata, github, assets)
            deleted = [call.args[0] for call in github.call_args_list if call.kwargs.get("method") == "DELETE"]
            self.assertEqual(deleted, ["/releases/assets/13", "/releases/assets/11"])
            self.assertEqual([call.args[0].split("?name=", 1)[1] for call in request.call_args_list], ["tool.tar.gz"])
            self.assertEqual(github.call_args_list[-1].kwargs["data"]["draft"], False)

    def test_ensure_github_release_keeps_unsealed_draft_assets_without_sealed_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            bundle = {"id": 10, "name": "release-npm.zip", "digest": "sha256:" + TOOLS.digest(archive)}
            other = {"id": 13, "name": "notes.txt", "digest": "sha256:" + "1" * 64}
            github = Mock(side_effect=[None, [self.draft([bundle, other])], None])
            request = self.run_ensure_release(temporary, archive, metadata, github)
            self.assertNotIn("DELETE", [call.kwargs.get("method") for call in github.call_args_list])
            request.assert_not_called()

    def test_ensure_github_release_checks_published_assets_without_replacing_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            metadata["assets"] = {"tool.tar.gz": TOOLS.hashlib.sha256(b"binary").hexdigest()}
            bundle = {"id": 10, "name": "release-npm.zip", "digest": "sha256:" + TOOLS.digest(archive)}
            exact = {"id": 11, "name": "tool.tar.gz", "digest": "sha256:" + metadata["assets"]["tool.tar.gz"]}
            unsealed = {"id": 12, "name": "added-later.txt", "digest": "sha256:" + "1" * 64}
            github = Mock(side_effect=[dict(self.draft([bundle, exact, unsealed]), draft=False)])
            request = self.run_ensure_release_with_assets(temporary, archive, metadata, github, {})
            request.assert_not_called()
            github.assert_called_once()
            published = dict(self.draft([bundle, exact]), draft=False)
            conflict = dict(exact, digest="sha256:" + "0" * 64)
            with self.assertRaisesRegex(ValueError, "tool.tar.gz differs; refusing to replace it"):
                self.run_ensure_release_with_assets(temporary, archive, metadata,
                                                    Mock(side_effect=[dict(published, assets=[bundle, conflict])]), {})
            with self.assertRaisesRegex(ValueError, "lacks sealed asset tool.tar.gz"):
                self.run_ensure_release_with_assets(temporary, archive, metadata,
                                                    Mock(side_effect=[dict(published, assets=[bundle])]), {})

    def test_ensure_github_release_rejects_an_asset_that_github_renamed(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            assets = self.asset_fixture(temporary, metadata, {"tool.tar.gz": b"binary"})
            environment = {"GITHUB_REPOSITORY": "BonfireTrunk/example", "GH_TOKEN": "token"}
            responses = [{"digest": "sha256:" + TOOLS.digest(archive)}, {"name": "tool.tar.gz.renamed"}]
            with patch.dict(os.environ, environment, clear=True), contextlib.chdir(temporary), \
                    patch.object(TOOLS, "tag_commit", return_value="release-commit"), \
                    patch.object(TOOLS, "github", Mock(side_effect=[None, [], self.draft()])), \
                    patch.object(TOOLS, "request", side_effect=responses):
                with self.assertRaisesRegex(ValueError, "GitHub renamed release asset tool.tar.gz"):
                    TOOLS.ensure_github_release(metadata, archive, assets)

    def test_ensure_github_release_leaves_a_complete_published_release_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            metadata, archive = self.release_fixture(temporary)
            asset = {"id": 11, "name": "release-npm.zip", "digest": "sha256:" + TOOLS.digest(archive)}
            github = Mock(side_effect=[dict(self.draft([asset]), draft=False)])
            request = self.run_ensure_release(temporary, archive, metadata, github)
            github.assert_called_once()
            request.assert_not_called()


class RequestTest(unittest.TestCase):
    def http_error(self, body):
        return TOOLS.urllib.error.HTTPError("https://crates.io/api/v1/crates/new", 400, "Bad Request", {}, io.BytesIO(body))

    def test_reports_registry_error_detail_only_when_asked(self):
        error = self.http_error(json.dumps({"errors": [{"detail": "missing license metadata"}]}).encode())
        with patch.object(TOOLS.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400: missing license metadata;"):
                TOOLS.request("https://crates.io/api/v1/crates/new", method="PUT", data=b"x", detail=True)
        error = self.http_error(json.dumps({"errors": [{"detail": "missing license metadata"}]}).encode())
        with patch.object(TOOLS.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 400; check access"):
                TOOLS.request("https://crates.io/api/v1/crates/new", method="PUT", data=b"x")

    def test_tolerates_an_error_body_that_is_not_json(self):
        with patch.object(TOOLS.urllib.request, "urlopen", side_effect=self.http_error(b"<html>")):
            with self.assertRaisesRegex(RuntimeError, r"HTTP 400; check access"):
                TOOLS.request("https://crates.io/api/v1/crates/new", method="PUT", data=b"x", detail=True)


class NpmBundleTest(unittest.TestCase):
    def test_bundles_the_single_archive_from_the_pack_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            temp = Path(temporary)
            (temp / "release-bundle").mkdir()
            (temp / "npm-pack").mkdir()
            content = io.BytesIO()
            manifest = json.dumps({"name": "@bonfiretrunk/example", "version": "1.2.3",
                                   "repository": "https://github.com/BonfireTrunk/example"}).encode()
            with TOOLS.tarfile.open(fileobj=content, mode="w:gz") as tar:
                entry = TOOLS.tarfile.TarInfo("package/package.json")
                entry.size = len(manifest)
                tar.addfile(entry, io.BytesIO(manifest))
            (temp / "npm-pack" / "bonfiretrunk-example-1.2.3.tgz").write_bytes(content.getvalue())
            (temp / "release-bundle" / "notes.md").write_text("- Release\n")
            TOOLS.write_json(temp / "release-bundle" / "metadata.json", {
                "kind": "npm", "sha": "release-commit", "version": "1.2.3", "tag": "v1.2.3", "name": "@bonfiretrunk/example"})
            environment = {"RUNNER_TEMP": temporary, "GITHUB_REPOSITORY": "BonfireTrunk/example"}
            with patch.dict(os.environ, environment, clear=True):
                TOOLS.bundle()
            self.assertEqual((temp / "release-bundle" / "package.tgz").read_bytes(), content.getvalue())
            self.assertTrue((temp / "release-npm.zip").is_file())
            (temp / "npm-pack" / "second-1.2.3.tgz").write_bytes(b"x")
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(ValueError, "Expected one npm package archive"):
                    TOOLS.bundle()


class ConcurrencyRenderTest(unittest.TestCase):
    def test_only_release_events_share_the_repository_release_queue(self):
        for kind in ("go", "npm", "rust"):
            with self.subTest(kind=kind):
                workflow = RENDER.render(kind)
                group = workflow.split("concurrency:\n  group: >-\n", 1)[1].split("  cancel-in-progress", 1)[0]
                self.assertIn(RENDER.RELEASE_EVENT, group)
                self.assertIn("format('package-release-skip-{0}', github.run_id)", group)
                self.assertIn("  cancel-in-progress: false\n  queue: max\n", workflow)
                self.assertIn("    if: >-\n      " + RENDER.RELEASE_EVENT + "\n", workflow)

    def test_rust_build_and_release_share_toolchain_validation_and_packaging(self):
        step = RENDER.RUST_STEP.format(condition="")
        self.assertIn(step, RENDER.render_rust_build())
        self.assertIn("cargo \"+$TOOLCHAIN\" package --locked", step)
        release = RENDER.render("rust")
        self.assertIn(RENDER.RUST_STEP.format(condition="        if: steps.prepare.outputs.restored != 'true'\n"), release)


if __name__ == "__main__":
    unittest.main()
