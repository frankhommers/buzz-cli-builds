"""Offline contract tests; fixtures are never represented as upstream test runs."""
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build

PIN = {
    "repository": "block/buzz", "tag": "desktop-v0.5.23",
    "commit": "b9392d9d78744df365f9276e1ffe8c1baa5ea903",
    "rust": "1.95.0", "version": "0.5.23-1",
    "rust_image": "rust:1.95.0-alpine@sha256:" + "a" * 64,
}
LINUX = "x86_64-unknown-linux-musl"


class PinTests(unittest.TestCase):
    def test_valid_pin(self):
        self.assertEqual(build.validate_pin(dict(PIN)), PIN)

    def test_missing_unknown_and_wrong_types(self):
        for key in PIN:
            for value in (None, True, 7, [], {}):
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    build.validate_pin(dict(PIN, **{key: value}))
            bad = dict(PIN)
            del bad[key]
            with self.assertRaises(ValueError):
                build.validate_pin(bad)
        with self.assertRaises(ValueError):
            build.validate_pin(dict(PIN, extra="no"))

    def test_unsafe_and_inconsistent_pins(self):
        for key, value in [
            ("repository", "https://github.com/block/buzz"),
            ("repository", "block/../buzz"), ("repository", "evil/buzz"),
            ("tag", "../desktop-v0.5.23"), ("tag", "desktop-v0.5.22"),
            ("commit", "A" * 40), ("commit", "b" * 39),
            ("rust", "stable"), ("version", "../../oops"),
            ("version", "0.5.23-1\n"), ("rust_image", "rust:1.95.0-alpine"),
            ("rust_image", "evil:1.95.0-alpine@sha256:" + "a" * 64),
            ("rust_image", "rust:1.94.0-alpine@sha256:" + "a" * 64),
        ]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                build.validate_pin(dict(PIN, **{key: value}))

    def test_duplicate_json_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "pin.json"
            p.write_text('{"repository":"block/buzz","repository":"evil/buzz"}')
            with self.assertRaises(ValueError):
                build.load_pin(p)

    def test_native_guards(self):
        build.check_native(LINUX, system="Linux", machine="x86_64")
        build.check_native("aarch64-apple-darwin", system="Darwin", machine="arm64")
        build.check_native("x86_64-pc-windows-msvc", system="Windows", machine="AMD64")
        for target, system, machine in [(LINUX, "Darwin", "x86_64"),
                                         (LINUX, "Linux", "aarch64"),
                                         ("../evil", "Linux", "x86_64")]:
            with self.assertRaises(ValueError):
                build.check_native(target, system=system, machine=machine)


class VerificationTests(unittest.TestCase):
    def test_rust_summary(self):
        text = "test foo ... ok\ntest result: ok. 463 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.24s\n"
        self.assertEqual(build.parse_test_summary(text), dict(passed=463, failed=0, ignored=0, filtered=0))
        for bad in ("", text.replace("463 passed", "0 passed"),
                    text.replace("0 ignored", "1 ignored"),
                    text.replace("0 filtered", "1 filtered"),
                    text.replace("0 failed", "1 failed"), text + text):
            with self.subTest(text=bad), self.assertRaises(ValueError):
                build.parse_test_summary(bad)

    def test_smoke_json_and_help_gates(self):
        self.assertTrue(build.check_smoke(["--help"], 0, 0, "Usage: buzz [OPTIONS]")["passed"])
        error = '{"error":"user_error","retryable":false,"message":"bad command"}'
        self.assertTrue(build.check_smoke(["definitely-not-a-command"], 1, 1, error)["passed"])
        for rc, out in [(2, error), (1, error.replace("false", "true")),
                        (1, error.replace("false", "0")), (1, "not json")]:
            with self.assertRaises(ValueError):
                build.check_smoke(["definitely-not-a-command"], 1, rc, out)
        with self.assertRaises(ValueError):
            build.check_smoke(["--help"], 0, 0, "nothing")

    def test_sanitized_environment(self):
        env = build.clean_env({"PATH": "/bin", "BUZZ_API_TOKEN": "secret", "buzz_relay": "bad",
                               "GITHUB_TOKEN": "secret", "GH_TOKEN": "secret", "RUSTFLAGS": "bad",
                               "GIT_CONFIG_COUNT": "1", "CARGO_HOME": "/cargo"})
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["CARGO_HOME"], "/cargo")
        for key in ("BUZZ_API_TOKEN", "buzz_relay", "GITHUB_TOKEN", "GH_TOKEN", "RUSTFLAGS", "GIT_CONFIG_COUNT"):
            self.assertNotIn(key, env)

    def test_static_elf_parser(self):
        data = bytearray(128)
        data[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<H", data, 18, 62)
        struct.pack_into("<Q", data, 32, 64)
        struct.pack_into("<HH", data, 54, 56, 1)
        struct.pack_into("<I", data, 64, 1)
        self.assertTrue(build.inspect_elf(bytes(data), LINUX)["static_linked"])
        with self.assertRaises(ValueError):
            build.inspect_elf(bytes(data), "aarch64-unknown-linux-musl")
        struct.pack_into("<I", data, 64, 3)
        with self.assertRaises(ValueError):
            build.inspect_elf(bytes(data), LINUX)
        struct.pack_into("<I", data, 64, 2)
        struct.pack_into("<Q", data, 72, 120)
        struct.pack_into("<Q", data, 96, 16)
        data.extend(b"\0" * 16)
        struct.pack_into("<qQ", data, 120, 1, 0)
        with self.assertRaises(ValueError):
            build.inspect_elf(bytes(data), LINUX)
        with self.assertRaises(ValueError):
            build.inspect_elf(b"not ELF", LINUX)

    def test_macho_header(self):
        for target, cpu in [("x86_64-apple-darwin", 0x1000007), ("aarch64-apple-darwin", 0x100000c)]:
            data = struct.pack("<II", 0xfeedfacf, cpu) + b"\0" * 24
            self.assertTrue(build.inspect_macho(data, target)["architecture_verified"])
            with self.assertRaises(ValueError):
                build.inspect_macho(data, LINUX)

    def test_pe_header_rejects_wrong_arch(self):
        data = bytearray(512)
        data[:2] = b"MZ"
        struct.pack_into("<I", data, 60, 128)
        data[128:132] = b"PE\0\0"
        struct.pack_into("<HH", data, 132, 0x8664, 0)
        struct.pack_into("<H", data, 148, 240)
        struct.pack_into("<H", data, 152, 0x20b)
        self.assertEqual(build.inspect_pe(bytes(data))["dependencies"], [])
        struct.pack_into("<H", data, 132, 0xaa64)
        with self.assertRaises(ValueError):
            build.inspect_pe(bytes(data))


    def test_pe_imports_and_static_crt(self):
        def pe_with_dependency(name):
            data = bytearray(1024)
            data[:2] = b"MZ"
            struct.pack_into("<I", data, 60, 128)
            data[128:132] = b"PE\0\0"
            struct.pack_into("<HH", data, 132, 0x8664, 1)
            struct.pack_into("<H", data, 148, 240)
            struct.pack_into("<H", data, 152, 0x20b)
            struct.pack_into("<I", data, 260, 16)
            struct.pack_into("<II", data, 272, 0x1000, 40)
            struct.pack_into("<IIII", data, 400, 512, 0x1000, 512, 512)
            struct.pack_into("<IIIII", data, 512, 0, 0, 0, 0x1064, 0)
            dll = name.encode("ascii") + b"\0"
            data[612:612 + len(dll)] = dll
            return data
        actual = build.inspect_pe(bytes(pe_with_dependency("KERNEL32.dll")))
        self.assertEqual(actual["dependencies"], ["KERNEL32.dll"])
        self.assertTrue(actual["static_crt"])
        self.assertEqual(build.inspect_pe(bytes(pe_with_dependency("combase.dll")))["dependencies"], ["combase.dll"])
        for name in ("VCRUNTIME140.dll", "ucrtbase.dll", "api-ms-win-crt-stdio-l1-1-0.dll", "unshipped.dll"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                build.inspect_pe(bytes(pe_with_dependency(name)))
        delayed = pe_with_dependency("KERNEL32.dll")
        struct.pack_into("<I", delayed, 152 + 112 + 13 * 8, 0x1000)
        with self.assertRaises(ValueError):
            build.inspect_pe(bytes(delayed))

    def test_command_logs_survive_nonzero_exit_and_append(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "command.log"
            first = build.run([sys.executable, "-c", "print('first')"], log=log)
            self.assertEqual(first.stdout.strip(), "first")
            with self.assertRaises(RuntimeError):
                build.run([sys.executable, "-c", "print('failure evidence'); raise SystemExit(7)"], log=log)
            text = log.read_text()
            self.assertIn("first", text)
            self.assertIn("failure evidence", text)
            self.assertIn("[exit_code=7]", text)

    def test_license_supplement_hashes_and_paths(self):
        manifest = build.load_license_supplements(ROOT / "license-supplements", "1.95.0")
        self.assertIn("nostr-0.44.7", manifest["crate_overrides"])
        with self.assertRaises(ValueError):
            build.load_license_supplements(ROOT / "license-supplements", "1.96.0")
        with tempfile.TemporaryDirectory() as d:
            import shutil
            folder = Path(d) / "supplements"
            shutil.copytree(ROOT / "license-supplements", folder)
            (folder / "nostr-0.44.7-LICENSE").write_text("tampered")
            with self.assertRaises(ValueError):
                build.load_license_supplements(folder, "1.95.0")

    def test_actual_registry_license_files_and_missing_report(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            logs, source = root / "logs", root / "source"
            logs.mkdir()
            source.mkdir()
            paths = []
            for name in ("has-notices", "missing-notices"):
                crate = root / "registry/src" / (name + "-1.0.0")
                crate.mkdir(parents=True)
                manifest = crate / "Cargo.toml"
                manifest.write_text(f'[package]\nname="{name}"\nversion="1.0.0"\nlicense="MIT"\n')
                paths.append(manifest)
                if name == "has-notices":
                    (crate / "licenses").mkdir()
                    (crate / "LICENSE-MIT").write_text("actual fixture license bytes\n")
                    (crate / "licenses" / "third-party.txt").write_text("actual fixture nested notice\n")
            records = "\n".join(json.dumps(dict(reason="compiler-artifact", manifest_path=str(p))) for p in paths)
            (logs / "test-preparation.log").write_text(records)
            (logs / "build.log").write_text(records)
            destination = root / "packaged-licenses"
            report = build.collect_licenses(logs, source, destination)
            self.assertEqual(report["compiled_manifest_count"], 2)
            self.assertEqual(report["missing_license_files"], ["missing-notices-1.0.0"])
            self.assertEqual((destination / "has-notices-1.0.0/LICENSE-MIT").read_text(), "actual fixture license bytes\n")
            self.assertEqual((destination / "has-notices-1.0.0/licenses/third-party.txt").read_text(), "actual fixture nested notice\n")
            index = json.loads((destination / "index.json").read_text())
            self.assertEqual(len(index["dependencies"]), 2)


class PackageTests(unittest.TestCase):
    def metadata(self, target):
        return dict(schema_version=1, target=target, version=PIN["version"],
                    source_repository=PIN["repository"], source_tag=PIN["tag"],
                    source_commit=PIN["commit"], recipe_commit="f" * 40,
                    unit_tests=dict(passed=1, failed=0, ignored=0, filtered=0),
                    smoke_tests=[build.check_smoke(a, rc, rc, "Usage: buzz" if rc == 0 else
                                '{"error":"user_error","retryable":false}')
                                for a, rc in build.SMOKE_CASES],
                    architecture_verified=True, static_linked=target.endswith("linux-musl"),
                    authenticated_relay_test="not_performed", run_url=None)

    def test_real_archives_and_receipt(self):
        for target in build.TARGETS:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                stage = root / "stage"
                stage.mkdir()
                exe = "buzz.exe" if target.endswith("windows-msvc") else "buzz"
                (stage / exe).write_bytes(b"offline fixture, not a production binary\n")
                (stage / "LICENSE").write_text("test fixture license")
                # Cargo crate archives can preserve Unix-epoch notice dates.
                os.utime(stage / "LICENSE", (1, 1))
                (stage / "logs").mkdir()
                (stage / "logs" / "unit-tests.log").write_text("fixture")
                (stage / "licenses").mkdir()
                (stage / "licenses" / "index.json").write_text("[]")
                receipt = build.package_artifact(stage, root / "out", PIN, target, self.metadata(target))
                self.assertEqual(len(list((root / "out").iterdir())), 2)
                archive = root / "out" / receipt["archive"]
                self.assertEqual(build.sha256(archive), receipt["archive_sha256"])
                self.assertEqual(build.sha256(stage / exe), receipt["binary_sha256"])
                self.assertEqual(json.loads((root / "out" / f"build-{target}.json").read_text()), receipt)
                prefix = f'buzz-cli-{PIN["version"]}-{target}/'
                if archive.suffix == ".zip":
                    with zipfile.ZipFile(archive) as z:
                        names = z.namelist()
                        internal = json.loads(z.read(prefix + "BUILD-INFO.json"))
                else:
                    with tarfile.open(archive) as t:
                        names = t.getnames()
                        internal = json.load(t.extractfile(prefix + "BUILD-INFO.json"))
                        self.assertTrue(t.getmember(prefix + exe).mode & 0o111)
                self.assertIn(prefix + exe, names)
                self.assertTrue(all(n == prefix[:-1] or n.startswith(prefix) for n in names))
                self.assertEqual(internal, {k: v for k, v in receipt.items() if k != "archive_sha256"})

    def test_packager_rejects_links_and_backslash_members(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stage = root / "stage"
            stage.mkdir()
            for name in ("buzz", "LICENSE"):
                (stage / name).write_text("fixture")
            for name in ("logs", "licenses"):
                (stage / name).mkdir()
            link = stage / "licenses" / "link"
            try:
                link.symlink_to(stage / "LICENSE")
            except OSError:
                # Windows without Developer Mode cannot create symlinks.
                pass
            else:
                with self.assertRaises(ValueError):
                    build.package_artifact(stage, root / "out", PIN, LINUX, self.metadata(LINUX))
                link.unlink()
            if sys.platform != "win32":
                (stage / "licenses" / "unsafe\\name").write_text("fixture")
                with self.assertRaises(ValueError):
                    build.package_artifact(stage, root / "out", PIN, LINUX, self.metadata(LINUX))

    def test_packager_rejects_bad_metadata_and_nonempty_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            stage = root / "stage"
            stage.mkdir()
            for name in ("buzz", "LICENSE"):
                (stage / name).write_text("fixture")
            for field, value in [("recipe_commit", ""), ("architecture_verified", False),
                                 ("static_linked", False), ("unit_tests", {"passed": True}),
                                 ("smoke_tests", []), ("target", "../bad")]:
                with self.subTest(field=field), self.assertRaises(ValueError):
                    build.package_artifact(stage, root / "out", PIN, LINUX,
                                           dict(self.metadata(LINUX), **{field: value}))
            out = root / "out"
            out.mkdir(exist_ok=True)
            (out / "keep.txt").write_text("do not erase")
            with self.assertRaises(ValueError):
                build.package_artifact(stage, out, PIN, LINUX, self.metadata(LINUX))
            self.assertEqual((out / "keep.txt").read_text(), "do not erase")


if __name__ == "__main__":
    unittest.main()
