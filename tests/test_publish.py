"""Offline tests for per-target publishing against a fake `gh`."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import publish  # noqa: E402
import release  # noqa: E402
import test_release  # noqa: E402

REPO = "owner/buzz-cli-builds"
TAG = "v0.5.23-1"
RECIPE = "b" * 40


class FakeGh:
    def __init__(self, release_state=None, tag_commit=RECIPE):
        self.release, self.tag_commit, self.calls, self.files = release_state, tag_commit, [], {}

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "api" and args[1].endswith(f"/releases/tags/{TAG}"):
            if self.release is None:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, json.dumps(self.release), ""
        if args[0] == "api" and args[1].endswith(f"/git/ref/tags/{TAG}"):
            return 0, json.dumps({"object": {"type": "commit", "sha": self.tag_commit}}), ""
        if args[:2] == ["release", "create"]:
            self.release = {"draft": False, "prerelease": True, "assets": []}
            return 0, "", ""
        if args[:2] == ["release", "upload"]:
            path = Path(args[3])
            digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            self.release["assets"].append({"name": path.name, "digest": digest, "state": "uploaded"})
            return 0, "", ""
        if args[:2] == ["release", "edit"]:
            self.release["prerelease"] = "--prerelease=false" not in args
            self.release["latest"] = "--latest" in args
            return 0, "", ""
        raise AssertionError(f"unexpected gh call: {args}")

    def uploads(self):
        return [Path(c[3]).name for c in self.calls if c[:2] == ["release", "upload"]]


class PublishTests(unittest.TestCase):
    setUp = test_release.ReleaseTests.setUp
    save = test_release.ReleaseTests.save

    def target_dir(self, target):
        directory = self.root / ("only-" + target)
        directory.mkdir()
        for name in (release.archive_name(self.pin["version"], target), f"build-{target}.json"):
            shutil.copyfile(self.root / name, directory / name)
        return directory

    def rel(self, fake):
        return publish.Release(REPO, TAG, runner=fake)

    def test_single_target_validated_alone(self):
        target = "x86_64-unknown-linux-musl"
        paths = release.validate_single(self.target_dir(target), target, self.pin, self.recipe)
        self.assertEqual(sorted(p.name for p in paths),
                         sorted([release.archive_name(self.pin["version"], target), f"build-{target}.json"]))
        with self.assertRaises(ValueError):
            release.validate_single(self.root, target, self.pin, self.recipe)  # other targets present
        with self.assertRaises(ValueError):
            release.validate_single(self.target_dir("aarch64-apple-darwin"), target, self.pin, self.recipe)

    def test_prepare_creates_public_prerelease(self):
        fake = FakeGh()
        self.assertTrue(publish.prepare(self.rel(fake), RECIPE, "Buzz CLI 0.5.23-1"))
        create = next(c for c in fake.calls if c[:2] == ["release", "create"])
        self.assertIn("--prerelease", create)
        self.assertNotIn("--draft", create)
        self.assertEqual(create[create.index("--target") + 1], RECIPE)

    def test_prepare_resumes_partial_and_skips_complete(self):
        partial = {"draft": False, "prerelease": True, "assets": [{"name": "build-x.json"}]}
        self.assertTrue(publish.prepare(self.rel(FakeGh(partial)), RECIPE, "t"))
        complete = {"draft": False, "prerelease": False,
                    "assets": [{"name": "SHA256SUMS"}, {"name": "manifest.json"}]}
        self.assertFalse(publish.prepare(self.rel(FakeGh(complete)), RECIPE, "t"))
        # A final release from an older recipe commit is left alone instead of failing the run.
        self.assertFalse(publish.prepare(self.rel(FakeGh(complete, tag_commit="c" * 40)), RECIPE, "t"))
        with self.assertRaises(ValueError):
            publish.prepare(self.rel(FakeGh(partial, tag_commit="c" * 40)), RECIPE, "t")

    def test_upload_is_idempotent_and_never_overwrites(self):
        target = "x86_64-unknown-linux-musl"
        paths = release.validate_single(self.target_dir(target), target, self.pin, self.recipe)
        fake = FakeGh({"draft": False, "prerelease": True, "assets": []})
        self.rel(fake).upload(paths)
        self.assertEqual(sorted(fake.uploads()), sorted(p.name for p in paths))
        self.rel(fake).upload(paths)  # rerun: same digests, nothing uploaded again
        self.assertEqual(len(fake.uploads()), 2)
        fake.release["assets"][0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            self.rel(fake).upload(paths)

    def bundle(self):
        out = self.root / "bundle"
        release.assemble(self.root, out, dict(self.pin), self.recipe)
        return out

    def test_finalize_uploads_matrix_last_and_marks_latest(self):
        bundle = self.bundle()
        pin = dict(self.pin, tag_repository="block/buzz")
        fake = FakeGh({"draft": False, "prerelease": True, "assets": []})
        publish.finalize(self.rel(fake), bundle, pin, "")
        self.assertEqual(fake.uploads()[-2:], ["manifest.json", "SHA256SUMS"])
        self.assertEqual(set(fake.uploads()), {p.name for p in bundle.iterdir()})
        self.assertFalse(fake.release["prerelease"])
        self.assertTrue(fake.release["latest"])

    def test_finalize_rejects_foreign_assets(self):
        bundle = self.bundle()
        pin = dict(self.pin, tag_repository="block/buzz")
        fake = FakeGh({"draft": False, "prerelease": True,
                       "assets": [{"name": "extra.bin", "digest": "sha256:" + "0" * 64, "state": "uploaded"}]})
        with self.assertRaises(ValueError):
            publish.finalize(self.rel(fake), bundle, pin, "")
        self.assertFalse(any(c[:2] == ["release", "edit"] for c in fake.calls))

    def test_desktop_release_notes(self):
        self.assertIn("Buzz Desktop 1.0.5", publish.notes(
            {"version": "1.0.5-1", "tag_repository": "block/buzz-app", "tag": "desktop-v1.0.5",
             "commit": "a" * 40, "rust": "1.98.1"}, "https://example.invalid/run"))

    def test_invalid_repo_or_tag_rejected(self):
        for repo, tag in (("owner/../x", TAG), (REPO, "latest"), (REPO, "v1.0.5-1;rm")):
            with self.assertRaises(ValueError):
                publish.Release(repo, tag, runner=FakeGh())


if __name__ == "__main__":
    unittest.main()
