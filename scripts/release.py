#!/usr/bin/env python3
"""Validate a complete native build matrix and assemble a release. Never publishes."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
import zipfile

TARGETS = (
    "x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl",
    "aarch64-apple-darwin", "x86_64-apple-darwin", "x86_64-pc-windows-msvc",
)
SMOKE_CASES = ((["--help"], 0), (["messages", "--help"], 0),
               (["channels", "--help"], 0), (["definitely-not-a-command"], 1))
SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(data):
    return json.loads(data, object_pairs_hook=no_duplicate_keys)


def archive_name(version, target):
    require(isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*", version), "Invalid version")
    require(target in TARGETS, "Unknown target")
    return f"buzz-cli-{version}-{target}" + (".zip" if "windows" in target else ".tar.gz")


def check_archive(path, row):
    """Read members without extracting or executing any downloaded content."""
    name = row["archive"]
    prefix = name.removesuffix(".tar.gz").removesuffix(".zip")
    binary_name = prefix + ("/buzz.exe" if "windows" in row["target"] else "/buzz")
    required = {binary_name, prefix + "/LICENSE", prefix + "/BUILD-INFO.json"}
    found = {}
    seen = set()
    total = 0

    def inspect(member_name, size, mode, is_file, reader):
        nonlocal total
        normalized = member_name.rstrip("/")
        parts = PurePosixPath(normalized).parts
        require("\\" not in normalized and not normalized.startswith("/") and
                all(p not in ("..", ".") for p in normalized.split("/")) and
                parts and parts[0] == prefix, "Unsafe archive member")
        require(normalized not in seen, "Duplicate archive member")
        seen.add(normalized)
        require(0 <= size <= 256 * 1024 * 1024, "Oversized member")
        total += size
        require(total <= 1024 * 1024 * 1024, "Oversized archive")
        if is_file and normalized in required:
            data = reader()
            require(len(data) == size, "Truncated archive member")
            if normalized == binary_name:
                require(hashlib.sha256(data).hexdigest() == row["binary_sha256"], "Binary checksum mismatch")
                if "windows" not in row["target"]:
                    require(mode & 0o111, "Binary is not executable")
            found[normalized] = data

    if name.endswith(".zip"):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                mode = member.external_attr >> 16
                require(not stat.S_ISLNK(mode), "Symlink in archive")
                require(stat.S_IFMT(mode) in (0, stat.S_IFREG, stat.S_IFDIR), "Special file in archive")
                inspect(member.filename, member.file_size, mode, not member.is_dir(),
                        lambda m=member: archive.read(m))
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                require(member.isfile() or member.isdir(), "Link or special file in archive")
                def read_member(m=member):
                    stream = archive.extractfile(m)
                    if stream is None:
                        raise ValueError("Missing member data")
                    return stream.read()
                inspect(member.name, member.size, member.mode, member.isfile(), read_member)
    require(set(found) == required, "Missing binary, license or build information")
    require(b"Apache License" in found[prefix + "/LICENSE"], "Upstream license missing")
    internal = load_json(found[prefix + "/BUILD-INFO.json"])
    for key in ("target", "version", "source_repository", "source_tag", "source_commit", "recipe_commit",
                "binary_sha256", "unit_tests", "smoke_tests", "architecture_verified", "static_linked"):
        require(internal.get(key) == row[key], f"Internal/external build information differs: {key}")


def validate_inputs(directory, pin, recipe_commit):
    require(COMMIT.fullmatch(recipe_commit or ""), "Invalid recipe commit")
    require(pin.get("repository") == "block/buzz", "Unexpected upstream repository")
    require(COMMIT.fullmatch(pin.get("commit", "")), "Invalid source commit")
    files = {}
    for path in directory.rglob("*"):
        require(not path.is_symlink(), "Symlink in build inputs")
        if path.is_file():
            require(path.name not in files, "Duplicate artifact filename")
            files[path.name] = path
    expected = set()
    for target in TARGETS:
        expected.update((archive_name(pin["version"], target), f"build-{target}.json"))
    require(set(files) == expected, f"Incomplete or unexpected build matrix: missing={sorted(expected-set(files))}; extra={sorted(set(files)-expected)}")
    rows = {}
    for target in TARGETS:
        row = load_json(files[f"build-{target}.json"].read_bytes())
        require(isinstance(row, dict), "Receipt is not an object")
        require(type(row.get("schema_version")) is int and row["schema_version"] == 1, "Invalid receipt schema")
        for key, value in {
            "target": target, "version": pin["version"], "source_repository": pin["repository"],
            "source_tag": pin["tag"], "source_commit": pin["commit"], "recipe_commit": recipe_commit,
            "archive": archive_name(pin["version"], target), "authenticated_relay_test": "not_performed",
        }.items():
            require(row.get(key) == value, f"Mismatching {target} receipt: {key}")
        require(row.get("architecture_verified") is True, "Architecture not verified")
        if "linux" in target:
            require(row.get("static_linked") is True, "Linux binary is not static")
        counts = row.get("unit_tests", {})
        require(isinstance(counts, dict), "Missing test counts")
        for key in ("passed", "failed", "ignored", "filtered"):
            require(type(counts.get(key)) is int, f"Invalid test count: {key}")
        require(counts["passed"] > 0 and all(counts[key] == 0 for key in ("failed", "ignored", "filtered")), "Unit tests incomplete or failing")
        smokes = row.get("smoke_tests")
        require(isinstance(smokes, list) and len(smokes) == len(SMOKE_CASES), "Missing smoke cases")
        for item, (args, code) in zip(smokes, SMOKE_CASES):
            require(isinstance(item, dict) and item.get("args") == args and
                    type(item.get("expected_exit")) is int and item["expected_exit"] == code and
                    type(item.get("actual_exit")) is int and item["actual_exit"] == code and
                    item.get("passed") is True, "Smoke case failed or mismatched")
        for key in ("archive_sha256", "binary_sha256"):
            require(isinstance(row.get(key), str) and SHA.fullmatch(row[key]), "Invalid checksum")
        require(sha256(files[row["archive"]]) == row["archive_sha256"], "Archive checksum mismatch")
        check_archive(files[row["archive"]], row)
        rows[target] = row
    return rows


def assemble(source, destination, pin, recipe_commit):
    rows = validate_inputs(source, pin, recipe_commit)
    require(not destination.exists(), "Refusing to overwrite release output")
    destination.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        if path.is_file():
            shutil.copyfile(path, destination / path.name)
    manifest = {"schema_version": 1, "version": pin["version"], "upstream": pin,
                "recipe_commit": recipe_commit, "builds": rows}
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in sorted(destination.iterdir()))
    (destination / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    print(json.dumps({"validated_targets": list(rows), "release_directory": str(destination),
                      "asset_count": len(list(destination.iterdir()))}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--recipe-commit", required=True)
    parser.add_argument("--pin", type=Path, default=Path(__file__).resolve().parents[1] / "upstream.json")
    args = parser.parse_args()
    assemble(args.input, args.output, load_json(args.pin.read_bytes()), args.recipe_commit)


if __name__ == "__main__":
    main()
