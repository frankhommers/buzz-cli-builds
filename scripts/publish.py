#!/usr/bin/env python3
"""Publish verified builds to a GitHub release, one target at a time.

The release is created as a public pre-release, each target's archive and receipt are
uploaded as soon as that target passes, and the release becomes final only after the
complete matrix (manifest.json and SHA256SUMS) is verified. Existing assets are never
overwritten: an asset that is already present must have the same SHA-256 digest.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
import release  # noqa: E402

require = release.require


def gh(args, input_text=None):
    result = subprocess.run(["gh", *args], input=input_text, capture_output=True, text=True, timeout=300)
    return result.returncode, result.stdout, result.stderr


class Release:
    def __init__(self, repo, tag, runner=gh):
        require(release.re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo), "Invalid repository")
        require(release.re.fullmatch(r"v" + build.VERSION_PATTERN, tag), "Invalid release tag")
        self.repo, self.tag, self.run = repo, tag, runner

    def call(self, args):
        code, out, err = self.run(args)
        require(code == 0, f"gh {' '.join(args[:3])} failed: {err.strip()[:300]}")
        return out

    def state(self):
        code, out, err = self.run(["api", f"repos/{self.repo}/releases/tags/{self.tag}"])
        if code != 0:
            require("404" in err or "Not Found" in err, f"Cannot read release: {err.strip()[:300]}")
            return None
        return release.load_json(out)

    def tag_commit(self):
        ref = release.load_json(self.call(["api", f"repos/{self.repo}/git/ref/tags/{self.tag}"]))
        require(ref.get("object", {}).get("type") == "commit", "Release tag must point at a commit")
        return ref["object"]["sha"]

    def assets(self):
        state = self.state()
        require(state is not None, "Release does not exist")
        return {a["name"]: a for a in state.get("assets", [])}

    def upload(self, paths):
        existing = self.assets()
        for path in paths:
            digest = "sha256:" + release.sha256(path)
            if path.name in existing:
                require(existing[path.name].get("digest") == digest, f"Release asset differs: {path.name}")
                print(json.dumps({"asset": path.name, "already_published": True}))
                continue
            self.call(["release", "upload", self.tag, str(path), "--repo", self.repo])
            print(json.dumps({"asset": path.name, "uploaded": True, "digest": digest}))


def prepare(rel, recipe_commit, title):
    """Return True when targets should be published (new or resumable release)."""
    require(release.COMMIT.fullmatch(recipe_commit), "Invalid recipe commit")
    state = rel.state()
    if state is None:
        rel.call(["release", "create", rel.tag, "--repo", rel.repo, "--target", recipe_commit, "--prerelease",
                  "--title", title, "--notes", "Building; targets are added as they pass. Not final yet."])
        return True
    names = {a["name"] for a in state.get("assets", [])}
    if "SHA256SUMS" in names and "manifest.json" in names and not state.get("draft"):
        return False  # Already final (possibly from an older recipe commit): never touch it again.
    # Resuming a partial release only makes sense for the same recipe; a new packaging
    # revision needs a new version (e.g. 1.0.5-2).
    require(rel.tag_commit() == recipe_commit, "An existing release for this version uses another recipe commit")
    return True


def notes(pin, run_url):
    base = pin["version"].rsplit("-", 1)[0]
    if pin["tag_repository"] == "block/buzz-app":
        origin = (f"The `buzz` CLI bundled with Buzz Desktop {base} (`block/buzz-app` tag `{pin['tag']}`), "
                  f"built from unmodified upstream `block/buzz` commit `{pin['commit']}`.")
    else:
        origin = f"Built from unmodified upstream `block/buzz` commit `{pin['commit']}` ({pin['tag']})."
    return (f"{origin}\n\nRust {pin['rust']} and the pinned Cargo.lock. All five native targets passed library "
            "tests, isolated CLI smoke checks and release-matrix validation; Linux executables are static (musl). "
            "Archives include licenses, build receipts and test evidence; SHA256SUMS covers all release assets.\n\n"
            "These checks do not authenticate to a live relay.\n\n" + (f"Build: {run_url}\n" if run_url else ""))


def finalize(rel, bundle, pin, run_url):
    files = sorted(p for p in bundle.iterdir() if p.is_file())
    names = {p.name for p in files}
    require({"SHA256SUMS", "manifest.json"} <= names, "Release bundle is incomplete")
    # Targets first, the matrix files last: SHA256SUMS only appears once everything it lists is there.
    order = sorted(files, key=lambda p: (p.name in ("manifest.json", "SHA256SUMS"), p.name == "SHA256SUMS"))
    rel.upload(order)
    assets = rel.assets()
    require(set(assets) == names, f"Release assets differ from the bundle: {sorted(set(assets) ^ names)}")
    for path in files:
        require(assets[path.name].get("state") == "uploaded", f"Asset not uploaded: {path.name}")
        require(assets[path.name].get("digest") == "sha256:" + release.sha256(path), f"Digest mismatch: {path.name}")
    args = ["release", "edit", rel.tag, "--repo", rel.repo, "--notes", notes(pin, run_url)]
    if "-dev." in pin["version"]:
        args += ["--prerelease"]
    else:
        args += ["--prerelease=false", "--latest"]
    rel.call(args)
    print(json.dumps({"release": rel.tag, "final": True, "assets": len(files)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare", "target", "finalize"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--recipe-commit", required=True)
        p.add_argument("--pin", type=Path, default=Path(__file__).resolve().parents[1] / "upstream.json")
        if name == "target":
            p.add_argument("--target", required=True, choices=release.TARGETS)
            p.add_argument("--input", required=True, type=Path)
            p.add_argument("--dry-run", action="store_true")
        if name == "finalize":
            p.add_argument("--input", required=True, type=Path)
            p.add_argument("--run-url", default="")
    args = parser.parse_args()
    pin = build.load_pin(args.pin)
    rel = Release(args.repo, "v" + pin["version"])
    if args.command == "prepare":
        title = f"Buzz CLI {pin['version']}"
        publish = prepare(rel, args.recipe_commit, title)
        with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a", encoding="utf-8") as out:
            out.write(f"publish={'true' if publish else 'false'}\ntag={rel.tag}\n")
        print(json.dumps({"release": rel.tag, "publish": publish}))
    elif args.command == "target":
        paths = release.validate_single(args.input, args.target, pin, args.recipe_commit)
        if args.dry_run:
            print(json.dumps({"target": args.target, "validated": [p.name for p in paths], "dry_run": True}))
        else:
            rel.upload(paths)
    else:
        # The workflow assembles args.input with release.py in the same job, which validates the matrix.
        finalize(rel, args.input, pin, args.run_url)


if __name__ == "__main__":
    main()
