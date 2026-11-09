"""Shared source embedded by render.py into reusable workflows."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version(value, shorthand=False):
    if shorthand and re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        value += ".0"
    pattern = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    match = re.fullmatch(pattern, value)
    require(match is not None, "Use X.Y.Z or X.Y.Z-prerelease; title shorthand X.Y is allowed. Build metadata is not supported.")
    for part in (match[4] or "").split("."):
        require(not (part.isdigit() and len(part) > 1 and part.startswith("0")), "Prerelease numbers cannot have leading zeros")
    return value


def release_title(title):
    title = re.sub(r" \(#\d+\)$", "", title.splitlines()[0])
    require(title.startswith("Release "), "Expected a squash-merge commit titled Release X.Y.Z")
    return version(title[len("Release "):], shorthand=True)


def version_key(value):
    version(value)
    core, separator, suffix = value.partition("-")
    parts = tuple((0, int(part)) if part.isdigit() else (1, part) for part in suffix.split(".")) if separator else ()
    return (*map(int, core.split(".")), not separator, parts)


def check_npm_repository(package):
    repository = package.get("repository")
    url = repository.get("url") if isinstance(repository, dict) else repository
    expected = "https://github.com/" + os.environ["GITHUB_REPOSITORY"]
    require(url in {expected, expected + ".git", "git+" + expected + ".git"},
            "package.json repository must identify this GitHub repository with its canonical HTTPS URL")
    require(not isinstance(repository, dict) or not repository.get("directory"), "Only a root npm package is supported")


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def request(url, token=None, method="GET", data=None, missing=False, content_type="application/json", raw=False, detail=False):
    headers = {"Accept": "application/json", "User-Agent": "BonfireTrunk-release-workflows"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = content_type
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            body = response.read()
            return body if raw else json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        if missing and error.code == 404:
            return None
        reason = ""
        if detail:
            # Only for registries whose error bodies carry validation messages, never credentials.
            try:
                errors = json.loads(error.read()).get("errors", [])
                reason = ": " + "; ".join(str(e.get("detail", "")) for e in errors)[:500]
            except (ValueError, AttributeError):
                pass
        failure = RuntimeError(f"{method} {urllib.parse.urlsplit(url).hostname} returned HTTP {error.code}{reason}; check access and service status")
        failure.status = error.code
        raise failure from None


def github(path, **kwargs):
    return request("https://api.github.com/repos/" + os.environ["GITHUB_REPOSITORY"] + path,
                   token=os.environ.get("GH_TOKEN"), **kwargs)


def tag_commit(tag):
    obj = github("/git/ref/tags/" + urllib.parse.quote(tag, safe=""), missing=True)
    if obj is None:
        return None
    obj = obj["object"]
    for _ in range(5):
        if obj["type"] == "commit":
            return obj["sha"]
        require(obj["type"] == "tag", "Release tag does not identify a commit")
        obj = github("/git/tags/" + obj["sha"])["object"]
    raise ValueError("Too many nested annotated tags")


def published_release(tag):
    """Published releases by tag; drafts are only listed to tokens with write access."""
    return github("/releases/tags/" + urllib.parse.quote(tag, safe=""), missing=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def output(**values):
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def check_asset_names(names, kind):
    for name in names:
        # GitHub renames asset files with other characters, which would break download links.
        require(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", name) and name.lower() != "release-" + kind + ".zip",
                "Release asset names use letters, digits, '.', '_' and '-', start and end with a letter or digit, and cannot be release-" + kind + ".zip: " + name)
    require(len({name.lower() for name in names}) == len(names), "Release asset names must differ in more than letter case")


def asset_files(directory, kind):
    paths = sorted(Path(directory).iterdir())
    require(paths, "assets-command left no files in RELEASE_ASSETS_DIR")
    for path in paths:
        require(path.is_file() and not path.is_symlink(), "Release assets must be regular files directly in RELEASE_ASSETS_DIR: " + path.name)
    check_asset_names([path.name for path in paths], kind)
    return {path.name: digest(path) for path in paths}


def check_bundle(directory, kind, sha, expected_version=None):
    metadata = json.loads((directory / "metadata.json").read_text())
    require(metadata["kind"] == kind and metadata["sha"] == sha, "Artifact belongs to another release commit or ecosystem")
    version(metadata["version"])
    if expected_version:
        require(metadata["version"] == expected_version, "Artifact version differs from requested release")
    require(metadata["tag"] == "v" + metadata["version"], "Artifact tag mismatch")
    allowed = {"notes.md", "package.tgz", "package.crate", "upload.bin"}
    require(set(metadata["files"]).issubset(allowed), "Unexpected release artifact file")
    for name, sha256 in metadata["files"].items():
        require(digest(directory / name) == sha256, "Release artifact checksum mismatch: " + name)
    expected = {"notes.md"} | ({"package.tgz"} if kind == "npm" else {"package.crate", "upload.bin"} if kind == "rust" else set())
    require(set(metadata["files"]) == expected, "Incomplete release bundle")
    check_asset_names(list(metadata.get("assets", {})), kind)
    return metadata
