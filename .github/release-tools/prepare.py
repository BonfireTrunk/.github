def changelog_notes(text, release_version):
    pattern = rf"^## \[{re.escape(release_version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\s*$"
    sections = re.split(r"(?m)^## ", text)
    matches = [s.split("\n", 1)[1] for s in sections[1:]
               if re.fullmatch(pattern, "## " + s.split("\n", 1)[0]) and "\n" in s]
    require(len(matches) == 1 and matches[0].strip(), "Expected one nonempty CHANGELOG.md section: ## [X.Y.Z] - YYYY-MM-DD")
    return matches[0].strip() + "\n"


def prepare():
    kind = os.environ["KIND"]
    require(os.environ.get("RELEASE_ENVIRONMENT", "release").strip(), "release-environment must name a GitHub environment")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    ref = os.environ["GITHUB_REF"]
    default_ref = "refs/heads/" + event["repository"]["default_branch"]
    require(os.environ["GITHUB_EVENT_NAME"] in {"push", "workflow_dispatch"}, "Unsupported release event")
    require(not event["repository"]["private"], "These public-registry workflows require a public repository")
    require(ref == default_ref or (os.environ["GITHUB_EVENT_NAME"] == "workflow_dispatch" and ref.startswith("refs/tags/v")), "Release requires the default branch or a manual version-tag retry")
    sha = run("git", "rev-parse", "HEAD")
    require(sha == os.environ["GITHUB_SHA"], "Checkout differs from triggering commit")
    value = version(ref[len("refs/tags/v"):]) if ref.startswith("refs/tags/") else release_title(run("git", "log", "-1", "--format=%s"))
    require(release_title(run("git", "log", "-1", "--format=%s")) == value, "Release tag does not match the release commit title")
    comparison = github("/compare/" + sha + "..." + urllib.parse.quote(event["repository"]["default_branch"], safe=""))
    require(comparison["merge_base_commit"]["sha"] == sha, "Release commit must belong to the current default branch history")
    tag = "v" + value
    existing = tag_commit(tag)
    require(existing is None or existing == sha, "Version tag already points at another commit")
    dest = Path(os.environ["RUNNER_TEMP"]) / "release-bundle"
    dest.mkdir()
    release = github("/releases/tags/" + tag, missing=True)
    if release:
        require(existing == sha, "Existing release has no matching tag")
        assets = [a for a in release["assets"] if a["name"] == "release-" + kind + ".zip"]
        if assets:
            require(len(assets) == 1, "Duplicate release bundle assets")
            archive = dest.parent / ("release-" + kind + ".zip")
            subprocess.run(["gh", "release", "download", tag, "--repo", os.environ["GITHUB_REPOSITORY"], "--pattern", archive.name, "--dir", str(dest.parent)], check=True)
            expected_digest = assets[0].get("digest")
            require(expected_digest == "sha256:" + digest(archive), "GitHub release asset digest missing or mismatched")
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
                allowed = {"metadata.json", "notes.md", "package.tgz", "package.crate", "upload.bin"}
                require(len(names) == len(set(names)) and set(names).issubset(allowed), "Unexpected archived bundle paths")
                bundle.extractall(dest)
            check_bundle(dest, kind, sha, value)
            output(version=value, restored="true")
            return
    notes = changelog_notes(Path("CHANGELOG.md").read_text(), value)
    (dest / "notes.md").write_text(notes)
    name = ""
    if kind == "npm":
        package = json.loads(Path("package.json").read_text())
        check_npm_repository(package)
        require(not package.get("private") and not package.get("workspaces"), "Only one public root npm package is supported")
        name = package["name"]
        owner = os.environ["GITHUB_REPOSITORY"].split("/")[0].lower()
        require(re.fullmatch(r"@" + re.escape(owner) + r"/[a-z0-9][a-z0-9._-]*", name), "npm package must use the lowercase GitHub owner scope")
        require(package["version"] == value, "package.json version must equal the full release version")
        lock = json.loads(Path("package-lock.json").read_text())
        require(lock.get("version") == value and lock.get("packages", {}).get("", {}).get("version") == value, "package-lock.json root version must match")
        require(not package.get("publishConfig") or package["publishConfig"] == {"access": "public"}, "publishConfig may only set access: public; registry and channel are controlled by the workflow")
    elif kind == "rust":
        manifest = tomllib.loads(Path("Cargo.toml").read_text())
        require("workspace" not in manifest and "package" in manifest, "Only one root crate, without a workspace, is supported")
        package = manifest["package"]
        name = package["name"]
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name), "Invalid crate name")
        require(package["version"] == value, "Cargo.toml version must equal the full release version")
        require(package.get("publish") in (None, ["crates-io"]), "Crate must allow crates.io publishing")
        require(Path("Cargo.lock").is_file(), "Commit Cargo.lock for repeatable release builds")
    else:
        require(kind == "go", "Unsupported ecosystem")
        require(not Path("go.work").exists(), "Only one root Go module is supported")
        match = re.search(r'(?m)^module\s+(?:"([^"\n]+)"|([^\s]+))', Path("go.mod").read_text())
        require(match is not None, "Missing go.mod module path")
        name = match[1] or match[2]
        major = int(value.split(".")[0])
        expected = "github.com/" + os.environ["GITHUB_REPOSITORY"] + (f"/v{major}" if major >= 2 else "")
        require(name == expected, "Go module path must match the repository and include /vN for major versions >= 2")
    write_json(dest / "metadata.json", {"kind": kind, "sha": sha, "version": value, "tag": tag, "name": name})
    output(version=value, restored="false")


if __name__ == "__main__":
    prepare()
