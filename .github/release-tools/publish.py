import struct


def npm_packument(metadata, registry, token=None):
    # GitHub Packages answers per-version and dist-tag paths with HTTP 405; both registries serve the full document.
    packument = request(registry + "/" + urllib.parse.quote(metadata["name"], safe=""), token=token, missing=True)
    if packument is not None:
        require(packument.get("name") == metadata["name"], "Registry returned another package identity")
    return packument


def registry_matches(metadata, registry, token=None):
    remote = (npm_packument(metadata, registry, token) or {}).get("versions", {}).get(metadata["version"])
    if remote is None:
        return False
    require(remote.get("name") == metadata["name"] and remote.get("version") == metadata["version"], "Registry returned another package identity")
    integrity = remote.get("dist", {}).get("integrity", "")
    actual = "sha512-" + base64.b64encode(hashlib.sha512(Path("package.tgz").read_bytes()).digest()).decode()
    require(integrity == actual, "Existing registry version has different bytes or no SHA-512 integrity; refusing to overwrite or skip it")
    return True


def check_npm_channel(metadata, registry, token=None):
    channel = "next" if "-" in metadata["version"] else "latest"
    current = (npm_packument(metadata, registry, token) or {}).get("dist-tags", {}).get(channel)
    if current:
        require(version_key(metadata["version"]) >= version_key(current), "Publishing this version would move npm " + channel + " backwards")


def npm_publish(metadata, registry, token=None):
    if registry_matches(metadata, registry, token):
        return
    check_npm_channel(metadata, registry, token)
    env = os.environ.copy()
    config = Path(os.environ["RUNNER_TEMP"]) / "publish.npmrc"
    config.write_text(("//npm.pkg.github.com/:_authToken=${NODE_AUTH_TOKEN}\n" if token else "") + "registry=" + registry + "\n")
    env["NPM_CONFIG_USERCONFIG"] = str(config)
    env["NPM_CONFIG_GLOBALCONFIG"] = str(config.parent / "empty-global.npmrc")
    env["NPM_CONFIG_CACHE"] = str(config.parent / "publish-cache")
    env.pop("NODE_AUTH_TOKEN", None)
    env.pop("NPM_TOKEN", None)
    if token:
        env["NODE_AUTH_TOKEN"] = token
    channel = "next" if "-" in metadata["version"] else "latest"
    result = subprocess.run(["npm", "publish", "package.tgz", "--ignore-scripts", "--registry", registry,
                             "--access", "public", "--tag", channel], env=env)
    # A lost response may follow an accepted upload. Reconcile before any retry;
    # npmjs.com lets its CDN serve a package document for up to five minutes.
    for attempt in range(24):
        if registry_matches(metadata, registry, token):
            return
        if attempt < 23:
            time.sleep(15)
    raise RuntimeError(f"npm publish exit {result.returncode}; version not confirmed. Rerun this job after checking registry status.")


def ensure_github_release(metadata, archive):
    commit = tag_commit(metadata["tag"])
    require(commit is None or commit == metadata["sha"], "Existing tag points at another commit")
    if commit is None:
        github("/git/refs", method="POST", data={"ref": "refs/tags/" + metadata["tag"], "sha": metadata["sha"]})
    prerelease = "-" in metadata["version"]
    release = published_release(metadata["tag"])
    if release is None:
        # Immutable releases reject assets once published, so attach the bundle to a draft first.
        drafts = [r for r in github("/releases?per_page=100") if r["draft"] and r["tag_name"] == metadata["tag"]]
        require(len(drafts) <= 1, "Several draft releases use this tag; delete the extra drafts")
        release = drafts[0] if drafts else github("/releases", method="POST", data={
            "tag_name": metadata["tag"], "target_commitish": metadata["sha"], "name": metadata["version"],
            "body": Path("notes.md").read_text(), "prerelease": prerelease, "draft": True})
    require(release["prerelease"] == prerelease, "Existing GitHub release has incompatible prerelease state")
    assets = [a for a in release["assets"] if a["name"] == archive.name]
    require(len(assets) <= 1, "Duplicate release bundle assets")
    if assets and assets[0].get("digest") != "sha256:" + digest(archive):
        # A draft is unpublished, so a rebuilt bundle may replace one left by an interrupted run.
        require(release["draft"], "Existing GitHub release bundle differs; refusing to replace it")
        github("/releases/assets/" + str(assets[0]["id"]), method="DELETE")
        assets = []
    if not assets:
        upload = release["upload_url"].split("{", 1)[0] + "?name=" + urllib.parse.quote(archive.name)
        uploaded = request(upload, token=os.environ.get("GH_TOKEN"), method="POST", data=archive.read_bytes(), content_type="application/zip")
        require(uploaded.get("digest") in (None, "sha256:" + digest(archive)), "GitHub stored different release bundle bytes")
    if release["draft"]:
        github("/releases/" + str(release["id"]), method="PATCH",
               data={"draft": False, "make_latest": "false" if prerelease else "legacy"})


def cargo_matches(metadata):
    name = metadata["name"].lower()
    path = "1/" + name if len(name) == 1 else "2/" + name if len(name) == 2 else "3/" + name[0] + "/" + name if len(name) == 3 else name[:2] + "/" + name[2:4] + "/" + name
    req = urllib.request.Request("https://index.crates.io/" + path, headers={"User-Agent": "BonfireTrunk-release-workflows"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            entries = [json.loads(line) for line in response.read().splitlines()]
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return False
        raise RuntimeError(f"crates.io index returned HTTP {error.code}") from None
    for entry in entries:
        if entry["vers"] == metadata["version"]:
            require(not entry.get("yanked"), "Existing crate version was yanked; refusing to treat it as published")
            require(entry["cksum"] == digest("package.crate"), "Existing crate version has different bytes")
            return True
    return False


def cargo_publish(metadata):
    if cargo_matches(metadata):
        return
    payload = Path("upload.bin").read_bytes()
    length = struct.unpack("<I", payload[:4])[0]
    info = json.loads(payload[4:4 + length])
    crate_length = struct.unpack("<I", payload[4 + length:8 + length])[0]
    require(info["name"] == metadata["name"] and info["vers"] == metadata["version"], "Cargo upload metadata mismatch")
    require(payload[8 + length:] == Path("package.crate").read_bytes() and crate_length == len(payload[8 + length:]), "Cargo upload does not contain the verified crate")
    error = None
    try:
        result = request("https://crates.io/api/v1/crates/new", token=os.environ["CARGO_REGISTRY_TOKEN"],
                         method="PUT", data=payload, content_type="application/octet-stream", detail=True)
        require(not result.get("errors"), "crates.io rejected the upload: " + "; ".join(str(e.get("detail", "")) for e in result["errors"])[:500])
    except RuntimeError as exc:
        error = str(exc)
        print(error)
    except (urllib.error.URLError, TimeoutError) as exc:
        error = type(exc).__name__
    for attempt in range(12):
        if cargo_matches(metadata):
            return
        if attempt < 11:
            time.sleep(10)
    raise RuntimeError("Crate upload not confirmed in index" + (" after " + error if error else "") + "; inspect crates.io and rerun this job")


def go_proxy_url(metadata, path):
    return "https://proxy.golang.org/" + re.sub(r"[A-Z]", lambda match: "!" + match[0].lower(), metadata["name"]) + "/@v/" + path


def go_proxy_known(metadata):
    # Asking for an untagged version makes the proxy hide that version for up to 30 minutes; the list does not.
    listing = request(go_proxy_url(metadata, "list"), missing=True, raw=True)
    return listing is not None and metadata["tag"] in listing.decode().split()


def go_proxy_info(metadata):
    info = request(go_proxy_url(metadata, metadata["tag"] + ".info"), missing=True)
    if info is not None:
        require(info.get("Version") == metadata["tag"], "Go proxy returned another version")
        require(info.get("Origin", {}).get("Hash") == metadata["sha"], "Go proxy has different or unverifiable source commit; never move a published tag")
    return info


def publish():
    kind = os.environ["KIND"]
    archive = Path(os.environ["RUNNER_TEMP"]) / "incoming" / ("release-" + kind + ".zip")
    dest = Path(os.environ["RUNNER_TEMP"]) / "publish"
    dest.mkdir()
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        require(len(names) == len(set(names)) and set(names).issubset({"metadata.json", "notes.md", "package.tgz", "package.crate", "upload.bin"}), "Unexpected artifact paths")
        zipped.extractall(dest)
    os.chdir(dest)
    metadata = check_bundle(dest, kind, os.environ["GITHUB_SHA"], os.environ["VERSION"])
    name = metadata["name"]
    owner = os.environ["GITHUB_REPOSITORY"].split("/")[0].lower()
    if kind == "npm":
        require(re.fullmatch(r"@" + re.escape(owner) + r"/[a-z0-9][a-z0-9._-]*", name), "Invalid npm name")
        with tarfile.open("package.tgz", "r:gz") as tar:
            package = json.loads(tar.extractfile("package/package.json").read())
        require(package["name"] == name and package["version"] == metadata["version"], "npm archive identity mismatch")
        check_npm_repository(package)
        require(not package.get("publishConfig") or package["publishConfig"] == {"access": "public"}, "npm archive overrides publishing policy")
    elif kind == "rust":
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name), "Invalid crate name")
    else:
        major = int(metadata["version"].split(".")[0])
        expected = "github.com/" + os.environ["GITHUB_REPOSITORY"] + (f"/v{major}" if major >= 2 else "")
        require(kind == "go" and name == expected, "Invalid Go module identity")
    commit = tag_commit(metadata["tag"])
    require(commit is None or commit == metadata["sha"], "Version tag already points elsewhere")
    if kind == "npm":
        # Detect a public-registry conflict before mutating GitHub Packages.
        if not registry_matches(metadata, "https://registry.npmjs.org"):
            check_npm_channel(metadata, "https://registry.npmjs.org")
        npm_publish(metadata, "https://npm.pkg.github.com", os.environ["GH_TOKEN"])
    elif kind == "rust":
        cargo_matches(metadata)
    elif commit is not None or go_proxy_known(metadata):
        go_proxy_info(metadata)
    ensure_github_release(metadata, archive)
    if kind == "npm":
        npm_publish(metadata, "https://registry.npmjs.org")
    elif kind == "rust":
        cargo_publish(metadata)
    else:
        env = os.environ.copy()
        env.update(GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org", GOPRIVATE="", GONOPROXY="", GONOSUMDB="", GOWORK="off", GOMODCACHE=str(dest / "gomodcache"))
        for attempt in range(12):
            result = subprocess.run(["go", "mod", "download", "-json", name + "@" + metadata["tag"]], env=env, capture_output=True, text=True)
            if result.returncode == 0:
                info = json.loads(result.stdout)
                require(info.get("Version") == metadata["tag"] and info.get("Sum") and info.get("GoModSum"), "Proxy response missing version/checksums")
                require(go_proxy_info(metadata) is not None, "Go proxy has no verifiable release metadata")
                print("Go module is available through proxy.golang.org with checksum verification")
                return
            if attempt < 11:
                time.sleep(10)
        raise RuntimeError("Tag/release exists, but Go proxy availability is not confirmed. Retry later; never move the tag.")


if __name__ == "__main__":
    publish()
