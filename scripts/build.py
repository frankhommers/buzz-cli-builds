#!/usr/bin/env python3
"""Build the pinned, unmodified Buzz CLI with native, fail-closed verification.

Python 3.11+, git; Linux additionally needs Docker/BuildKit. macOS/Windows
need rustup and native C/C++ build tools. No GitHub API or token is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "x86_64-unknown-linux-musl": ("Linux", "x86_64", "linux/amd64"),
    "aarch64-unknown-linux-musl": ("Linux", "aarch64", "linux/arm64"),
    "aarch64-apple-darwin": ("Darwin", "aarch64", None),
    "x86_64-apple-darwin": ("Darwin", "x86_64", None),
    "x86_64-pc-windows-msvc": ("Windows", "x86_64", None),
}
SMOKE_CASES = [(["--help"], 0), (["messages", "--help"], 0),
               (["channels", "--help"], 0), (["definitely-not-a-command"], 1)]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_pin(pin):
    keys = {"repository", "tag", "commit", "rust", "rust_image", "version"}
    require(type(pin) is dict and set(pin) == keys, "Pin must have exactly: " + ", ".join(sorted(keys)))
    require(all(type(v) is str for v in pin.values()), "All pin values must be strings")
    require(pin["repository"] == "block/buzz", "Only the public block/buzz source is allowed")
    require(re.fullmatch(r"[0-9a-f]{40}", pin["commit"]), "Commit must be a lowercase full SHA-1")
    semver = r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    require(re.fullmatch(semver, pin["rust"]), "Rust must be an exact stable version")
    require(re.fullmatch(semver + r"-[1-9][0-9]*", pin["version"]), "Unsafe release version")
    require(pin["tag"] == "desktop-v" + pin["version"].rsplit("-", 1)[0], "Tag/version mismatch")
    require(re.fullmatch(r"rust:" + re.escape(pin["rust"]) + r"-alpine@sha256:[0-9a-f]{64}",
                         pin["rust_image"]), "Rust image must be the matching official Alpine tag plus digest")
    return pin


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_pin(path):
    return validate_pin(json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique_object))


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def normalized_arch(machine):
    return {"amd64": "x86_64", "x86_64": "x86_64", "arm64": "aarch64",
            "aarch64": "aarch64"}.get(machine.lower(), machine.lower())


def check_native(target, *, system=None, machine=None):
    require(target in TARGETS, "Unsupported target")
    system = system or platform.system()
    machine = machine or platform.machine()
    expected_os, expected_arch, _ = TARGETS[target]
    require(system == expected_os and normalized_arch(machine) == expected_arch,
            f"Native {expected_os}/{expected_arch} required; found {system}/{machine}")
    if system == "Darwin" and platform.system() == "Darwin":
        translated = subprocess.run(["sysctl", "-in", "sysctl.proc_translated"],
                                    capture_output=True, text=True)
        require(translated.stdout.strip() != "1", "Rosetta translation is not a native x86_64 runner")


def clean_env(source=None):
    """Do not let caller credentials, Buzz settings, or Rust flags affect builds."""
    env = dict(os.environ if source is None else source)
    exact = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_API_TOKEN",
             "ACTIONS_RUNTIME_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "CARGO_ENCODED_RUSTFLAGS",
             "RUSTFLAGS", "RUSTDOCFLAGS", "RUSTC", "RUSTDOC", "RUSTC_WRAPPER", "RUSTC_WORKSPACE_WRAPPER",
             "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET", "RUSTUP_TOOLCHAIN", "MACOSX_DEPLOYMENT_TARGET"}
    for key in list(env):
        upper = key.upper()
        if upper in exact or upper.startswith(("BUZZ", "GIT_")) or (
                upper.startswith("CARGO_") and ("TOKEN" in upper or "RUSTFLAGS" in upper)):
            del env[key]
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               CARGO_TERM_COLOR="never", RUST_BACKTRACE="0")
    return env


def run(args, *, cwd=None, env=None, log=None, expected=0, timeout=7200):
    """Save real combined output, including failing commands, before checking status."""
    args = [str(a) for a in args]
    print("+ " + subprocess.list2cmdline(args), flush=True)
    if log:
        # Write directly to disk during compilation: CI cancellation/timeouts
        # must not discard hours of diagnostic evidence held only in memory.
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        with Path(log).open("ab", buffering=0) as f:
            f.write(("+ " + subprocess.list2cmdline(args) + "\n").encode("utf-8"))
            start = f.tell()
            child = subprocess.Popen(args, cwd=cwd, env=env, stdout=f, stderr=subprocess.STDOUT)
            try:
                child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
                f.write(b"\n[command timed out]\n")
                raise
        with Path(log).open("rb") as f:
            f.seek(start)
            output = f.read().decode("utf-8", errors="replace")
        with Path(log).open("a", encoding="utf-8") as f:
            f.write(f"\n[exit_code={child.returncode}]\n")
        proc = subprocess.CompletedProcess(args, child.returncode, output)
    else:
        proc = subprocess.run(args, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    # Cargo JSON logs contain large machine records; keep them in the artifact,
    # while emitting only diagnostics and test output to the CI console.
    for line in proc.stdout.splitlines():
        if not line.startswith('{"reason":'):
            print(line, flush=True)
    if expected is not None and proc.returncode != expected:
        raise RuntimeError(f"Command exited {proc.returncode}, expected {expected}; log: {log}")
    return proc


def parse_test_summary(text):
    matches = re.findall(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored; "
                         r"(\d+) measured; (\d+) filtered out;", text)
    require(len(matches) == 1, "Expected exactly one real buzz-cli library test summary")
    status, passed, failed, ignored, measured, filtered = matches[0]
    result = dict(zip(("passed", "failed", "ignored", "filtered"),
                      map(int, (passed, failed, ignored, filtered))))
    require(status == "ok" and result["passed"] > 0 and int(measured) == 0 and
            all(result[k] == 0 for k in ("failed", "ignored", "filtered")),
            f"Unit-test gate failed: {result}. Ignored upstream tests need explicit review, not a bypass.")
    return result


def check_smoke(args, expected, actual, output):
    require(actual == expected, f"Smoke {args}: exit {actual}, expected {expected}: {output}")
    if expected == 0:
        require("Usage:" in output, f"Smoke {args}: no help usage text")
    else:
        try:
            error = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ValueError("Malformed-command output is not JSON") from exc
        require(type(error) is dict and error.get("error") == "user_error" and
                error.get("retryable") is False, "Malformed command must be nonretryable user_error JSON")
    return {"args": list(args), "expected_exit": expected, "actual_exit": actual, "passed": True}


def inspect_elf(data, target):
    require(target in TARGETS and target.endswith("linux-musl"), "Not a Linux target")
    require(len(data) >= 64 and data[:6] == b"\x7fELF\x02\x01", "Not little-endian ELF64")
    expected_machine = 62 if target.startswith("x86_64") else 183
    require(struct.unpack_from("<H", data, 18)[0] == expected_machine, "ELF architecture mismatch")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    require(size >= 56 and count > 0 and offset + size * count <= len(data), "Invalid ELF program headers")
    for i in range(count):
        header = offset + i * size
        kind = struct.unpack_from("<I", data, header)[0]
        require(kind != 3, "ELF PT_INTERP is forbidden")
        if kind == 2:  # PT_DYNAMIC: static PIE may have this, but no DT_NEEDED.
            start = struct.unpack_from("<Q", data, header + 8)[0]
            length = struct.unpack_from("<Q", data, header + 32)[0]
            require(length % 16 == 0 and start + length <= len(data), "Invalid ELF dynamic table")
            for j in range(start, start + length, 16):
                tag = struct.unpack_from("<q", data, j)[0]
                require(tag != 1, "ELF DT_NEEDED is forbidden")
                if tag == 0:
                    break
    return {"architecture_verified": True, "static_linked": True,
            "format": "ELF64", "dependencies": [], "interpreter": None}


def inspect_macho(data, target):
    require(target in ("x86_64-apple-darwin", "aarch64-apple-darwin"), "Not a macOS target")
    require(len(data) >= 32 and data[:4] == b"\xcf\xfa\xed\xfe", "Not thin little-endian Mach-O 64")
    expected = 0x1000007 if target.startswith("x86_64") else 0x100000c
    require(struct.unpack_from("<I", data, 4)[0] == expected, "Mach-O architecture mismatch")
    return {"architecture_verified": True, "static_linked": False, "format": "Mach-O 64"}


def inspect_pe(data):
    require(len(data) >= 64 and data[:2] == b"MZ", "Not a PE file")
    pe = struct.unpack_from("<I", data, 60)[0]
    require(pe + 264 <= len(data) and data[pe:pe + 4] == b"PE\0\0", "Invalid PE signature/header")
    machine, sections = struct.unpack_from("<HH", data, pe + 4)
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    optional = pe + 24
    require(machine == 0x8664 and struct.unpack_from("<H", data, optional)[0] == 0x20b,
            "PE must be x86_64 PE32+")
    require(optional_size >= 240, "Truncated PE32+ optional header")
    section_table = optional + optional_size
    require(section_table + 40 * sections <= len(data), "Truncated PE sections")

    def rva_offset(rva):
        for i in range(sections):
            s = section_table + i * 40
            virtual_size, virtual_address, raw_size, raw_offset = struct.unpack_from("<IIII", data, s + 8)
            if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
                offset = raw_offset + rva - virtual_address
                require(rva - virtual_address < raw_size and offset < len(data), "Unbacked PE RVA")
                return offset
        raise ValueError(f"Unmapped PE RVA: {rva}")

    directories = struct.unpack_from("<I", data, optional + 108)[0]
    require(directories <= 16, "Invalid PE data directory count")
    deps = []
    if directories > 1:
        import_rva, import_size = struct.unpack_from("<II", data, optional + 120)
        if import_rva:
            p = rva_offset(import_rva)
            require(import_size >= 20 and p + import_size <= len(data), "Invalid PE import table")
            terminated = False
            for start in range(p, p + import_size - 19, 20):
                descriptor = struct.unpack_from("<IIIII", data, start)
                if not any(descriptor):
                    terminated = True
                    break
                name = rva_offset(descriptor[3])
                end = data.find(b"\0", name, min(name + 512, len(data)))
                require(end > name, "Invalid PE DLL name")
                deps.append(data[name:end].decode("ascii"))
            require(terminated, "Unterminated PE import table")
    if directories > 13:
        require(struct.unpack_from("<I", data, optional + 112 + 13 * 8)[0] == 0,
                "Delay-loaded DLLs require explicit review")
    system_dlls = {"advapi32.dll", "bcrypt.dll", "bcryptprimitives.dll", "cfgmgr32.dll", "comctl32.dll", "combase.dll",
                   "crypt32.dll", "cryptbase.dll", "dbghelp.dll", "dnsapi.dll", "gdi32.dll", "iphlpapi.dll",
                   "kernel32.dll", "kernelbase.dll", "ncrypt.dll", "netapi32.dll", "normaliz.dll", "ntdll.dll",
                   "ole32.dll", "oleaut32.dll", "powrprof.dll", "propsys.dll", "psapi.dll", "rpcrt4.dll",
                   "secur32.dll", "setupapi.dll", "shell32.dll", "shlwapi.dll", "user32.dll", "userenv.dll",
                   "version.dll", "winhttp.dll", "winmm.dll", "ws2_32.dll", "wtsapi32.dll"}
    for dep in deps:
        low = dep.lower()
        require(not low.startswith(("vcruntime", "msvcp", "msvcr", "ucrt", "api-ms-win-crt-")),
                f"Dynamic MSVC CRT dependency: {dep}")
        require(low in system_dlls or low.startswith(("api-ms-win-", "ext-ms-win-")),
                f"Non-system Windows dependency needs review: {dep}")
    return {"architecture_verified": True, "static_linked": False, "static_crt": True,
            "format": "PE32+", "dependencies": sorted(deps)}


def verify_binary(binary, target, logs, env):
    data = binary.read_bytes()
    if target.endswith("linux-musl"):
        result = inspect_elf(data, target)
        # The built-in parser enforces both PT_INTERP and DT_NEEDED; binutils is
        # additional human-readable evidence, not a text-substring safety gate.
        for args in (["file", binary], ["readelf", "-h", "-l", "-d", binary]):
            run(args, env=env, log=logs / "binary-inspection.log")
    elif target.endswith("apple-darwin"):
        result = inspect_macho(data, target)
        deps = run(["otool", "-L", binary], env=env, log=logs / "binary-inspection.log").stdout
        dependencies = [line.strip().split(" (", 1)[0] for line in deps.splitlines()[1:] if line.strip()]
        require(dependencies and all(x.startswith(("/usr/lib/", "/System/Library/")) for x in dependencies),
                f"Non-system macOS dependencies: {dependencies}")
        commands = run(["otool", "-l", binary], env=env, log=logs / "binary-inspection.log").stdout
        minimum = re.findall(r"\bminos\s+([0-9.]+)", commands)
        if not minimum:
            minimum = re.findall(r"cmd LC_VERSION_MIN_MACOSX\s+cmdsize \d+\s+version ([0-9.]+)", commands)
        require(len(minimum) == 1 and minimum[0] in ("11.0", "11.0.0"),
                f"Expected macOS deployment target 11.0, got {minimum}")
        result.update(dependencies=dependencies, deployment_target="11.0")
    else:
        result = inspect_pe(data)
        dumpbin = shutil.which("dumpbin")
        if not dumpbin:
            # Hosted Windows runners do not always put the MSVC tools on PATH.
            vswhere = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
            if vswhere.is_file():
                found = run([vswhere, "-latest", "-products", "*", "-find",
                             "VC/Tools/MSVC/**/bin/Hostx64/x64/dumpbin.exe"], env=env,
                            log=logs / "binary-inspection.log").stdout.splitlines()
                dumpbin = next((x.strip() for x in found if Path(x.strip()).is_file()), None)
        if dumpbin:
            run([dumpbin, "/HEADERS", "/DEPENDENTS", binary], env=env, log=logs / "binary-inspection.log")
        else:
            (logs / "binary-inspection.log").write_text(
                "dumpbin not found; PE32+ sections, import table and delay-import gate inspected by Python.\n",
                encoding="utf-8")
    write_json(logs / "binary-inspection.json", result)
    return result


def build_env(work, pin, target):
    env = clean_env()
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    # Preserve access to an already installed native rustup, but isolate user
    # configuration and source checkout credentials from the build/test HOME.
    old_home = Path.home()
    env.setdefault("RUSTUP_HOME", str(old_home / ".rustup"))
    env.setdefault("CARGO_HOME", str(old_home / ".cargo"))
    env.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_DATA_HOME=str(home / ".local/share"), APPDATA=str(home / "AppData/Roaming"),
               LOCALAPPDATA=str(home / "AppData/Local"),
               RUSTUP_TOOLCHAIN=pin["rust"], CARGO_TARGET_DIR=str(work / "cargo-target"),
               CARGO_PROFILE_RELEASE_STRIP="symbols")
    if target.endswith("apple-darwin"):
        env["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
    if target.endswith("windows-msvc"):
        env["RUSTFLAGS"] = "-C target-feature=+crt-static"
    return env


def verify_toolchain(pin, target, env, log, install):
    if install:
        installed = run(["rustup", "toolchain", "list"], env=env, log=log).stdout
        if not any(line.split()[0].startswith(pin["rust"] + "-") for line in installed.splitlines() if line.split()):
            run(["rustup", "toolchain", "install", pin["rust"], "--profile", "minimal", "--no-self-update"],
                env=env, log=log)
    info = run(["rustc", "-vV"], env=env, log=log).stdout
    release = re.search(r"^release: (.+)$", info, re.M)
    host = re.search(r"^host: (.+)$", info, re.M)
    require(release and release.group(1) == pin["rust"], "Actual Rust version differs from pin")
    require(host and host.group(1) == target, "Rust toolchain is not native to the selected target")
    cargo = run(["cargo", "--version"], env=env, log=log).stdout.strip()
    require(cargo.startswith("cargo " + pin["rust"] + " "), "Actual Cargo version differs from pin")
    return {"rustc": info.strip(), "cargo": cargo}


def fetch_source(source, pin, env, log):
    require(not source.exists(), f"Refusing to reuse a source checkout: {source}")
    source.mkdir(parents=True)
    git = ["git", "-c", "credential.helper=", "-c", "core.autocrlf=false", "-c", "core.longpaths=true",
           "-c", "core.hooksPath=" + os.devnull]
    run(git + ["init", str(source)], env=env, log=log)
    run(git + ["remote", "add", "origin", f'https://github.com/{pin["repository"]}.git'], cwd=source, env=env, log=log)
    run(git + ["fetch", "--depth=1", "--no-tags", "origin", pin["commit"]], cwd=source, env=env, log=log)
    run(git + ["checkout", "--detach", "FETCH_HEAD"], cwd=source, env=env, log=log)
    head = run(git + ["rev-parse", "HEAD"], cwd=source, env=env, log=log).stdout.strip()
    require(head == pin["commit"], "Fetched source commit differs from pin")
    run(git + ["fetch", "--depth=1", "--no-tags", "origin", f'refs/tags/{pin["tag"]}:refs/tags/{pin["tag"]}'],
        cwd=source, env=env, log=log)
    tag = run(git + ["rev-parse", f'refs/tags/{pin["tag"]}^{{commit}}'], cwd=source, env=env, log=log).stdout.strip()
    require(tag == pin["commit"], "Source tag does not peel to the pinned commit")
    toolchain = tomllib.loads((source / "rust-toolchain.toml").read_text(encoding="utf-8"))
    require(toolchain.get("toolchain", {}).get("channel") == pin["rust"], "Upstream Rust toolchain pin mismatch")
    require((source / "LICENSE").is_file() and (source / "Cargo.lock").is_file(), "Missing upstream license/lockfile")
    return sha256(source / "Cargo.lock")


def source_unchanged(source, lock_hash, env, log):
    status = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=source, env=env, log=log).stdout
    require(not status.strip(), "Tracked upstream source was modified")
    require(sha256(source / "Cargo.lock") == lock_hash, "Cargo.lock changed")


def cargo_command(target, operation):
    return ["cargo", operation, "--locked", "--release", "-p", "buzz-cli", "--target", target]


def load_license_supplements(directory, rust_version):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    require(manifest["rust_version"] == rust_version, "Runtime license inventory needs review for this Rust version")
    for name, entry in manifest["files"].items():
        require(re.fullmatch(r"[A-Za-z0-9._-]+", name) and name not in (".", ".."), "Unsafe license supplement name")
        path = directory / name
        require(path.is_file() and not path.is_symlink() and sha256(path) == entry["sha256"], "License supplement integrity failure")
    for names in list(manifest["crate_overrides"].values()) + [manifest["runtime_files"]]:
        require(names and all(name in manifest["files"] for name in names), "Unknown license supplement reference")
    return manifest


def collect_licenses(logs, source, destination):
    """Use compiler-artifact manifests, not workspace cargo metadata or a guessed list."""
    manifests = set()
    for path in (logs / "test-preparation.log", logs / "build.log"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith('{"reason":'):
                continue
            record = json.loads(line)
            if record.get("reason") == "compiler-artifact" and record.get("manifest_path"):
                manifests.add(Path(record["manifest_path"]).resolve())
    require(manifests, "Cargo emitted no compiled dependency manifests")
    destination.mkdir(parents=True, exist_ok=True)
    inventory, missing = [], []
    supplements = ROOT / "license-supplements"
    supplement_manifest = load_license_supplements(supplements, load_pin(ROOT / "upstream.json")["rust"])
    shutil.copytree(supplements, destination / "supplemental-sources")
    for manifest in sorted(manifests):
        meta = tomllib.loads(manifest.read_text(encoding="utf-8"))
        pkg = meta["package"]
        crate = manifest.parent
        if crate.is_relative_to(source.resolve()):
            # Workspace crates share the unmodified upstream LICENSE.
            inventory.append({"name": pkg["name"], "source": "workspace", "license_file": "../LICENSE"})
            continue
        name, version = pkg["name"], pkg["version"]
        require(type(name) is str and re.fullmatch(r"[A-Za-z0-9_-]+", name), "Unsafe dependency name")
        require(type(version) is str and re.fullmatch(r"[A-Za-z0-9.+_-]+", version), "Unsafe dependency version")
        key = name + "-" + version
        out = destination / key
        require(not out.exists(), f"Ambiguous compiled dependency identity: {key}")
        out.mkdir()
        shutil.copy2(manifest, out / "Cargo.toml")
        files = []
        explicit = pkg.get("license-file")
        for path in sorted(crate.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(crate)
            if any(re.match(r"^(licen[sc]es?|copying|notices?|copyright)(?:[._-]|$)", p, re.I) for p in relative.parts) or (
                    explicit and relative.as_posix() == explicit):
                dest = out / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                files.append(relative.as_posix())
        supplemental = []
        if not files:
            for filename in supplement_manifest["crate_overrides"].get(key, []):
                shutil.copy2(supplements / filename, out / filename)
                files.append(filename)
                supplemental.append(supplement_manifest["files"][filename])
        entry = {"supplemental_sources": supplemental, "name": name, "version": version, "license": pkg.get("license"),
                 "repository": pkg.get("repository"), "files": files, "manifest_sha256": sha256(manifest)}
        inventory.append(entry)
        if not files:
            missing.append(key)
    limitation = ("Conservative compiled-dependency superset including library-test/build dependencies. "
                  "Cargo license files are copied; missing texts use hash-verified, version-scoped upstream supplements. "
                  "Rust/LLVM/musl notices are included conservatively; operating-system SDK terms still apply. "
                  "This inventory is not a legal compliance certification.")
    report = {"scope": limitation, "dependencies": inventory, "missing_license_files": missing}
    write_json(destination / "index.json", report)
    if missing:
        print("NOTICE: compiled crates with no shipped license text: " + ", ".join(missing), flush=True)
    return {"inventory": "licenses/index.json", "compiled_manifest_count": len(manifests),
            "missing_license_files": missing, "limitations": limitation}


def prepare(work, pin, target, install):
    logs = work / "logs"
    logs.mkdir(parents=True)
    env = build_env(work, pin, target)
    tools = verify_toolchain(pin, target, env, logs / "toolchain.log", install)
    source = work / "source"
    lock = fetch_source(source, pin, env, logs / "source.log")
    # Select only buzz-cli. Avoid cargo fetch/metadata for the huge full workspace.
    run(cargo_command(target, "test") + ["--lib", "--no-run", "--message-format=json-render-diagnostics"],
        cwd=source, env=env, log=logs / "test-preparation.log")
    source_unchanged(source, lock, env, logs / "source.log")
    write_json(work / "preparation.json", {"toolchain": tools, "cargo_lock_sha256": lock})


def test_library(work, pin, target):
    env = build_env(work, pin, target)
    result = run(cargo_command(target, "test") + ["--offline", "--lib", "--", "--test-threads=3"],
                 cwd=work / "source", env=env, log=work / "logs/unit-tests.log")
    write_json(work / "unit-tests.json", parse_test_summary(result.stdout))


def build_binary(work, pin, target, artifact):
    env = build_env(work, pin, target)
    source, logs = work / "source", work / "logs"
    prepared = json.loads((work / "preparation.json").read_text(encoding="utf-8"))
    run(cargo_command(target, "build") + ["--offline", "--bin", "buzz", "--message-format=json-render-diagnostics"],
        cwd=source, env=env, log=logs / "build.log")
    source_unchanged(source, prepared["cargo_lock_sha256"], env, logs / "source.log")
    artifact.mkdir(parents=True)
    exe = "buzz.exe" if target.endswith("windows-msvc") else "buzz"
    shutil.copy2(work / "cargo-target" / target / "release" / exe, artifact / exe)
    (artifact / exe).chmod(0o755)
    shutil.copy2(source / "LICENSE", artifact / "LICENSE")
    shutil.copy2(source / "Cargo.lock", artifact / "Cargo.lock")
    inspection = verify_binary(artifact / exe, target, logs, env)
    licenses = collect_licenses(logs, source, artifact / "licenses")
    require(not licenses["missing_license_files"], "Compiled dependencies need license text before distribution")
    shutil.copytree(logs, artifact / "logs")
    result = {**prepared, **inspection, "dependency_licenses": licenses,
              "unit_tests": json.loads((work / "unit-tests.json").read_text(encoding="utf-8")),
              "source_unmodified": True, "cargo_locked": True, "library_test_profile": "release",
              "library_tests_network_disabled": target.endswith("linux-musl")}
    write_json(artifact / "verification.json", result)
    return result


def docker_command():
    command = ["docker"]
    check = subprocess.run(command + ["info", "--format", "{{.OSType}}/{{.Architecture}}"], capture_output=True, text=True)
    if check.returncode and os.name != "nt" and shutil.which("sudo"):
        command = ["sudo", "-n", "docker"]
        check = subprocess.run(command + ["info", "--format", "{{.OSType}}/{{.Architecture}}"], capture_output=True, text=True)
    require(check.returncode == 0, "Docker daemon unavailable (also tried passwordless sudo); no configuration changed")
    return command, check.stdout.strip()


def linux_build(work, pin, target):
    docker, daemon = docker_command()
    parts = daemon.split("/")
    require(len(parts) == 2 and parts[0] == "linux" and normalized_arch(parts[1]) == TARGETS[target][1],
            f"Docker daemon is not native to {target}: {daemon}")
    artifact = work / "artifact"
    run(docker + ["buildx", "build", "--progress=plain", "--platform", TARGETS[target][2],
                  "--build-arg", "RUST_IMAGE=" + pin["rust_image"], "--build-arg", "BUZZ_TARGET=" + target,
                  "--target", "artifact", "--output", "type=local,dest=" + str(artifact), str(ROOT)],
        env=clean_env(), log=work / "docker-build.log")
    if docker[:2] == ["sudo", "-n"]:
        # Only fix the output of our own BuildKit export, never the checkout.
        run(["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", artifact],
            env=clean_env(), log=work / "docker-build.log")
    shutil.copy2(work / "docker-build.log", artifact / "logs/docker-build.log")
    result = json.loads((artifact / "verification.json").read_text(encoding="utf-8"))
    # Re-inspect exported bytes on the host as well as in the native builder.
    require(inspect_elf((artifact / "buzz").read_bytes(), target)["static_linked"], "Exported ELF verification failed")
    result["builder_image"] = pin["rust_image"]
    result["docker_native_platform"] = daemon
    return artifact, result, docker


def smoke_tests(artifact, target, work, docker=None):
    env = clean_env()
    results = []
    tag = "buzz-cli-smoke:" + uuid.uuid4().hex
    if docker:
        context = work / "smoke-context"
        context.mkdir()
        shutil.copy2(artifact / "buzz", context / "buzz")
        (context / "Dockerfile").write_text(
            'FROM scratch\nCOPY --chmod=0755 buzz /buzz\nUSER 65534:65534\nENTRYPOINT ["/buzz"]\n', encoding="utf-8")
        run(docker + ["buildx", "build", "--network=none", "--platform", TARGETS[target][2],
                      "--load", "-t", tag, str(context)], env=env, log=artifact / "logs/smoke-image.log")
    try:
        for index, (args, expected) in enumerate(SMOKE_CASES):
            with tempfile.TemporaryDirectory(prefix="smoke-home-", dir=work) as home:
                clean = dict(env, HOME=home, USERPROFILE=home, XDG_CONFIG_HOME=home,
                             XDG_DATA_HOME=home, APPDATA=home, LOCALAPPDATA=home)
                if docker:
                    command = docker + ["run", "--rm", "--platform", TARGETS[target][2],
                                        "--network=none", "--read-only", "--user", "65534:65534",
                                        "--cap-drop=ALL", "--security-opt=no-new-privileges", "--pids-limit=64",
                                        "--memory=256m", "--cpus=1", "--env", "HOME=/tmp",
                                        "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m,mode=1777", tag]
                else:
                    command = [str(artifact / ("buzz.exe" if target.endswith("windows-msvc") else "buzz"))]
                proc = run(command + args, cwd=home, env=clean, expected=None, timeout=60,
                           log=artifact / f"logs/smoke-{index}.log")
                results.append(check_smoke(args, expected, proc.returncode, proc.stdout))
    finally:
        if docker:
            run(docker + ["image", "rm", tag], env=env, log=artifact / "logs/smoke-image.log", timeout=60)
    return results


def validate_metadata(metadata, pin, target):
    require(type(metadata) is dict and type(metadata.get("schema_version")) is int and
            metadata["schema_version"] == 1, "Invalid receipt schema_version")
    for key, expected in (("target", target), ("version", pin["version"]),
                          ("source_repository", pin["repository"]), ("source_tag", pin["tag"]),
                          ("source_commit", pin["commit"])):
        require(metadata.get(key) == expected, f"Receipt {key} mismatch")
    require(type(metadata.get("recipe_commit")) is str and re.fullmatch(r"[0-9a-f]{40}", metadata["recipe_commit"]),
            "Recipe must have a real git HEAD commit")
    require(metadata.get("architecture_verified") is True, "Architecture was not verified")
    static = metadata.get("static_linked")
    require(static is None or type(static) is bool, "static_linked must be bool/null")
    if target.endswith("linux-musl"):
        require(static is True, "Linux binary must be static")
    tests = metadata.get("unit_tests")
    require(type(tests) is dict and set(tests) == {"passed", "failed", "ignored", "filtered"}, "Invalid unit-test counts")
    require(all(type(n) is int and n >= 0 for n in tests.values()) and tests["passed"] > 0 and
            all(tests[k] == 0 for k in ("failed", "ignored", "filtered")), "Unit-test receipt gate failed")
    smokes = metadata.get("smoke_tests")
    require(type(smokes) is list and len(smokes) == len(SMOKE_CASES), "Incomplete smoke tests")
    for result, (args, expected) in zip(smokes, SMOKE_CASES):
        require(type(result) is dict and result.get("args") == args and result.get("passed") is True and
                type(result.get("expected_exit")) is int and type(result.get("actual_exit")) is int and
                result["expected_exit"] == expected and result["actual_exit"] == expected, "Smoke receipt gate failed")
    require(metadata.get("authenticated_relay_test") == "not_performed", "Unexpected relay test claim")
    url = metadata.get("run_url")
    require(url is None or (type(url) is str and re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+", url)), "Unsafe run URL")
    require("archive_sha256" not in metadata, "Recursive archive digest is forbidden")


def package_artifact(stage, output, pin, target, metadata):
    validate_pin(pin)
    require(target in TARGETS, "Unsafe target")
    validate_metadata(metadata, pin, target)
    stage, output = Path(stage).resolve(), Path(output).resolve()
    require(not output.is_relative_to(stage) and not stage.is_relative_to(output), "Stage/output overlap")
    require(not output.exists() or not any(output.iterdir()), "Output directory must be empty; refusing to erase artifacts")
    exe = "buzz.exe" if target.endswith("windows-msvc") else "buzz"
    for name in (exe, "LICENSE"):
        require((stage / name).is_file(), f"Missing package file: {name}")
    for name in ("logs", "licenses"):
        require((stage / name).is_dir(), f"Missing package directory: {name}")
    for path in stage.rglob("*"):
        relative = path.relative_to(stage).as_posix()
        require("\\" not in relative and all(part not in (".", "..") for part in relative.split("/")),
                f"Unsafe package member: {relative}")
        require(not path.is_symlink(), f"Package symlinks are forbidden: {path}")
        require(path.is_file() or path.is_dir(), f"Special package file is forbidden: {path}")
    base = f'buzz-cli-{pin["version"]}-{target}'
    archive_name = base + (".zip" if target.endswith("windows-msvc") else ".tar.gz")
    inside = dict(metadata, archive=archive_name, binary_sha256=sha256(stage / exe))
    write_json(stage / "BUILD-INFO.json", inside)
    (stage / exe).chmod(0o755)
    output.mkdir(parents=True, exist_ok=True)
    # Write under work/stage first; successful output contains only archive + receipt.
    temporary = stage.parent / (archive_name + ".partial")
    try:
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for path in sorted(stage.rglob("*")):
                    if path.is_file():
                        archive.write(path, base + "/" + path.relative_to(stage).as_posix())
        else:
            with tarfile.open(temporary, "w:gz", compresslevel=9, format=tarfile.PAX_FORMAT) as archive:
                def normalize(info):
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    return info
                archive.add(stage, arcname=base, filter=normalize)
        receipt = dict(inside, archive_sha256=sha256(temporary))
        shutil.move(str(temporary), output / archive_name)
        write_json(output / f"build-{target}.json", receipt)
    finally:
        temporary.unlink(missing_ok=True)
    require({p.name for p in output.iterdir()} == {archive_name, f"build-{target}.json"}, "Unexpected output files")
    return receipt


def github_run_url():
    repo, run_id = os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
    if repo and run_id:
        require(os.environ.get("GITHUB_SERVER_URL", "https://github.com") == "https://github.com", "Not a public GitHub run")
        url = f"https://github.com/{repo}/actions/runs/{run_id}"
        require(re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+", url), "Invalid GitHub run metadata")
        return url
    return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--container-phase", choices=("prepare", "test", "build"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    pin = load_pin(ROOT / "upstream.json")
    check_native(args.target)
    if args.container_phase:
        require(platform.system() == "Linux" and args.target.endswith("linux-musl") and os.environ.get("BUILD_CONTAINER") == "1",
                "Internal phases require the Linux Docker builder")
        work = ROOT / "work/native"
        if args.container_phase == "prepare":
            prepare(work, pin, args.target, install=False)
        elif args.container_phase == "test":
            test_library(work, pin, args.target)
        else:
            build_binary(work, pin, args.target, ROOT / "artifact")
        return 0
    require(args.output is not None, "--output is required")
    output = args.output.resolve()
    require(output != ROOT / "dist" and output.is_relative_to(ROOT / "dist"), "Output must be a target directory beneath root/dist")
    require(not output.exists() or not any(output.iterdir()), "Output directory must be empty")
    # A receipt cannot invent a recipe identity for an uncommitted new repository.
    require(not run(["git", "status", "--porcelain", "--untracked-files=normal"],
                    cwd=ROOT, env=clean_env()).stdout.strip(), "Commit the recipe changes before building")
    recipe = run(["git", "rev-parse", "HEAD"], cwd=ROOT, env=clean_env()).stdout.strip()
    require(re.fullmatch(r"[0-9a-f]{40}", recipe), "No valid recipe git HEAD")
    work_root = ROOT / "work"
    work_root.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=args.target + "-", dir=work_root))
    print(f"Build work/logs retained at {work}", flush=True)
    docker = None
    if args.target.endswith("linux-musl"):
        stage, verified, docker = linux_build(work, pin, args.target)
    else:
        prepare(work, pin, args.target, install=True)
        test_library(work, pin, args.target)
        stage = work / "artifact"
        verified = build_binary(work, pin, args.target, stage)
    smokes = smoke_tests(stage, args.target, work, docker)
    # Internal phase handoff is not an additional public receipt.
    (stage / "verification.json").unlink()
    metadata = dict(verified, schema_version=1, target=args.target, version=pin["version"],
                    source_repository=pin["repository"], source_tag=pin["tag"], source_commit=pin["commit"],
                    recipe_commit=recipe, smoke_tests=smokes, authenticated_relay_test="not_performed",
                    smoke_isolation=("scratch; nonroot; read-only root; network disabled; temporary tmpfs" if docker else
                                     "native; sanitized BUZZ environment; temporary HOME; no relay configuration"),
                    run_url=github_run_url())
    receipt = package_artifact(stage, output, pin, args.target, metadata)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
