import shutil
import struct


def crate_payload(archive):
    with tarfile.open(archive, "r:gz") as tar:
        manifests = [m for m in tar.getmembers() if m.name.count("/") == 1 and m.name.endswith("/Cargo.toml")]
        require(len(manifests) == 1, "Expected one packaged Cargo.toml")
        root = manifests[0].name.rsplit("/", 1)[0]
        manifest = tomllib.loads(tar.extractfile(manifests[0]).read().decode())
        package = manifest["package"]
        deps = []
        def dependencies(table, target=None):
            for section, kind in (("dependencies", "normal"), ("dev-dependencies", "dev"), ("build-dependencies", "build")):
                for alias, spec in table.get(section, {}).items():
                    if isinstance(spec, str):
                        spec = {"version": spec}
                    require("version" in spec and not any(k in spec for k in ("git", "path", "registry", "registry-index")), "Packaged dependencies must resolve from crates.io")
                    require(not any(k in spec for k in ("artifact", "target", "lib")), "Cargo artifact dependencies are not supported by this release workflow")
                    deps.append({"name": spec.get("package", alias), "version_req": spec["version"],
                                 "features": spec.get("features", []), "optional": spec.get("optional", False),
                                 "default_features": spec.get("default-features", True), "target": target,
                                 "kind": kind, "registry": None, "explicit_name_in_toml": alias if "package" in spec else None})
        dependencies(manifest)
        for target, table in manifest.get("target", {}).items():
            dependencies(table, target)
        metadata = {key: package.get(key) for key in ("name", "authors", "description", "documentation", "homepage", "keywords", "categories", "license", "repository", "links")}
        for key in ("authors", "keywords", "categories"):
            metadata[key] = package.get(key, [])
        metadata.update(vers=package["version"], deps=deps, features=manifest.get("features", {}),
                        license_file=package.get("license-file"), rust_version=package.get("rust-version"),
                        readme_file=package.get("readme") or None, readme=None, badges=manifest.get("badges", {}))
        if metadata["readme_file"]:
            require(isinstance(metadata["readme_file"], str), "Packaged readme must be a path")
            metadata["readme"] = tar.extractfile(root + "/" + metadata["readme_file"]).read().decode()
    encoded = json.dumps(metadata).encode()
    crate = archive.read_bytes()
    return metadata, struct.pack("<I", len(encoded)) + encoded + struct.pack("<I", len(crate)) + crate


def bundle():
    dest = Path(os.environ["RUNNER_TEMP"]) / "release-bundle"
    metadata = json.loads((dest / "metadata.json").read_text())
    kind = metadata["kind"]
    if kind == "npm":
        packed = list(Path(os.environ["RUNNER_TEMP"], "npm-pack").iterdir())
        require(len(packed) == 1 and packed[0].suffix == ".tgz" and packed[0].is_file(), "Expected one npm package archive")
        source = packed[0]
        shutil.copyfile(source, dest / "package.tgz")
        with tarfile.open(source, "r:gz") as tar:
            package = json.loads(tar.extractfile("package/package.json").read())
        require(package["name"] == metadata["name"] and package["version"] == metadata["version"], "Packaged npm identity changed during build")
        check_npm_repository(package)
        require(not package.get("publishConfig") or package["publishConfig"] == {"access": "public"}, "Packaged publishConfig overrides release policy")
    elif kind == "rust":
        archive = Path("target/package") / (metadata["name"] + "-" + metadata["version"] + ".crate")
        package, payload = crate_payload(archive)
        require(package["name"] == metadata["name"] and package["vers"] == metadata["version"], "Packaged crate identity changed during build")
        shutil.copyfile(archive, dest / "package.crate")
        (dest / "upload.bin").write_bytes(payload)
    metadata["files"] = {p.name: digest(p) for p in dest.iterdir() if p.name != "metadata.json"}
    if os.environ.get("RELEASE_ASSETS") == "true":
        # Only digests are sealed; the files travel beside the bundle so the release does not store them twice.
        metadata["assets"] = asset_files(Path(os.environ["RUNNER_TEMP"]) / "release-assets", kind)
    write_json(dest / "metadata.json", metadata)
    check_bundle(dest, kind, metadata["sha"], metadata["version"])
    archive = dest.parent / ("release-" + kind + ".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path in sorted(dest.iterdir()):
            zipped.write(path, path.name)


if __name__ == "__main__":
    bundle()
