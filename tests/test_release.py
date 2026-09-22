import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location("release", Path(__file__).parents[1] / "scripts/release.py")
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pin = {"repository": "block/buzz", "tag": "desktop-v0.5.23", "commit": "a" * 40,
                    "rust": "1.95.0", "version": "0.5.23-1"}
        self.recipe = "b" * 40
        self.rows = []
        for target in release.TARGETS:
            name = release.archive_name(self.pin["version"], target)
            prefix = name.removesuffix(".tar.gz").removesuffix(".zip")
            binary = b"unit-test fixture, not an executable"
            row = {"schema_version": 1, "target": target, "version": self.pin["version"],
                   "source_repository": self.pin["repository"], "source_tag": self.pin["tag"],
                   "source_commit": self.pin["commit"], "recipe_commit": self.recipe,
                   "archive": name, "binary_sha256": hashlib.sha256(binary).hexdigest(),
                   "unit_tests": {"passed": 1, "failed": 0, "ignored": 0, "filtered": 0},
                   "smoke_tests": [{"args": args, "expected_exit": code, "actual_exit": code, "passed": True}
                                   for args, code in release.SMOKE_CASES],
                   "architecture_verified": True, "static_linked": True if "linux" in target else None,
                   "authenticated_relay_test": "not_performed", "run_url": ""}
            contents = {prefix + "/" + ("buzz.exe" if "windows" in target else "buzz"): binary,
                        prefix + "/LICENSE": b"Apache License fixture",
                        prefix + "/BUILD-INFO.json": json.dumps(row).encode()}
            archive = self.root / name
            if name.endswith(".zip"):
                with zipfile.ZipFile(archive, "w") as out:
                    for key, data in contents.items(): out.writestr(key, data)
            else:
                with tarfile.open(archive, "w:gz") as out:
                    for key, data in contents.items():
                        info = tarfile.TarInfo(key)
                        info.size = len(data)
                        info.mode = 0o755 if key.endswith("/buzz") else 0o644
                        out.addfile(info, io.BytesIO(data))
            row["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.rows.append(row)
            self.save(row)

    def save(self, row):
        (self.root / ("build-" + row["target"] + ".json")).write_text(json.dumps(row))

    def validate(self):
        return release.validate_inputs(self.root, self.pin, self.recipe)

    def test_complete_matrix_accepted(self):
        rows = self.validate()
        self.assertEqual(set(rows), set(release.TARGETS))

    def test_missing_target_rejected(self):
        (self.root / ("build-" + self.rows[0]["target"] + ".json")).unlink()
        with self.assertRaises(ValueError): self.validate()

    def test_wrong_source_rejected(self):
        self.rows[0]["source_commit"] = "c" * 40
        self.save(self.rows[0])
        with self.assertRaises(ValueError): self.validate()

    def test_wrong_recipe_rejected(self):
        self.rows[0]["recipe_commit"] = "c" * 40
        self.save(self.rows[0])
        with self.assertRaises(ValueError): self.validate()

    def test_boolean_test_count_rejected(self):
        self.rows[0]["unit_tests"]["passed"] = True
        self.save(self.rows[0])
        with self.assertRaises(ValueError): self.validate()

    def test_skipped_tests_rejected(self):
        self.rows[0]["unit_tests"]["ignored"] = 1
        self.save(self.rows[0])
        with self.assertRaises(ValueError): self.validate()

    def test_missing_smoke_rejected(self):
        self.rows[0]["smoke_tests"].pop()
        self.save(self.rows[0])
        with self.assertRaises(ValueError): self.validate()

    def test_failed_smoke_rejected(self):
        self.rows[0]["smoke_tests"][0]["actual_exit"] = 1
        self.save(self.rows[0])
        with self.assertRaises(ValueError): self.validate()

    def test_modified_archive_rejected(self):
        p = self.root / self.rows[0]["archive"]
        p.write_bytes(p.read_bytes() + b"modified")
        with self.assertRaises(ValueError): self.validate()

    def test_unexpected_file_rejected(self):
        (self.root / "extra").write_text("untrusted")
        with self.assertRaises(ValueError): self.validate()

    def test_duplicate_receipt_rejected(self):
        nested = self.root / "duplicate"
        nested.mkdir()
        row = self.rows[0]
        (nested / ("build-" + row["target"] + ".json")).write_text(json.dumps(row))
        with self.assertRaises(ValueError): self.validate()

    def test_release_bundle_checksums_match(self):
        dest = self.root.parent / (self.root.name + "-out")
        self.addCleanup(lambda: __import__("shutil").rmtree(dest, ignore_errors=True))
        release.assemble(self.root, dest, self.pin, self.recipe)
        self.assertEqual(len(list(dest.iterdir())), 12)
        for line in (dest / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ")
            self.assertEqual(hashlib.sha256((dest / name).read_bytes()).hexdigest(), digest)

    def test_archive_path_traversal_rejected(self):
        row = self.rows[-1]
        self.assertTrue(row["archive"].endswith(".zip"))
        p = self.root / row["archive"]
        with zipfile.ZipFile(p, "a") as out: out.writestr("../bad", b"bad")
        row["archive_sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
        self.save(row)
        with self.assertRaises(ValueError): self.validate()


if __name__ == "__main__":
    unittest.main()
