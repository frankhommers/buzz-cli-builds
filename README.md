# Buzz CLI builds

[![Build Buzz CLI](https://github.com/frankhommers/buzz-cli-builds/actions/workflows/build.yml/badge.svg)](https://github.com/frankhommers/buzz-cli-builds/actions/workflows/build.yml)

**Unofficial, standalone command-line builds of [Block's Buzz](https://github.com/block/buzz).**
No desktop application, relay installation, Rust installation, or Docker installation is needed to use the downloaded CLI.
This project is not affiliated with or endorsed by Block. Buzz source code is built without modifications.

## Download

Get the archive for your operating system from [Releases](https://github.com/frankhommers/buzz-cli-builds/releases/latest).

| Platform | Target | Archive |
| --- | --- | --- |
| Linux x86_64 | `x86_64-unknown-linux-musl` | `.tar.gz` |
| Linux ARM64 | `aarch64-unknown-linux-musl` | `.tar.gz` |
| macOS Apple Silicon | `aarch64-apple-darwin` | `.tar.gz` |
| macOS Intel | `x86_64-apple-darwin` | `.tar.gz` |
| Windows x86_64 | `x86_64-pc-windows-msvc` | `.zip` |

Each published version must pass native builds, CLI library tests and smoke tests on **all five targets**.
Linux executables use musl and must have no ELF interpreter or shared-library dependencies.
macOS and Windows executables are built and tested on their native GitHub-hosted runners, not cross-compiled inside Linux containers.

Extract the archive and run `./buzz --help` (Linux/macOS) or `.\buzz.exe --help` (PowerShell).
For Linux/macOS, optionally install it into a user-owned directory on your PATH:

```sh
mkdir -p "$HOME/.local/bin"
install -m 755 buzz "$HOME/.local/bin/buzz"
```

Normal system CA certificates are still needed for TLS connections. Downloads include no keys, credentials or community configuration.
Configure your Buzz identity and relay separately, following the [upstream documentation](https://github.com/block/buzz).
Some upstream CLI versions do not implement `--version`; use the included build information for provenance.

### Integrity and provenance

Each release includes:

- A platform archive containing the executable, upstream license, dependency license material and build/test evidence.
- One `build-<target>.json` receipt per target, with source and build-recipe commits, binary/archive SHA-256 hashes and real test results.
- `manifest.json` describing the complete matrix.
- `SHA256SUMS` covering all other release assets.

Download the checksum file and the archive into the same directory. Compare the archive's hash with its entry in `SHA256SUMS`:

```sh
sha256sum buzz-cli-*.tar.gz        # Linux
shasum -a 256 buzz-cli-*.tar.gz    # macOS
```

```powershell
Get-FileHash .\buzz-cli-*.zip -Algorithm SHA256
```

When you downloaded **all** release assets, `sha256sum -c SHA256SUMS` verifies the complete set.
Checksums detect corruption; they do not provide an independent publisher signature.

### Signing and test limitations

- No Apple Developer ID notarization or Windows publisher certificate is supplied. Platform download/security warnings are possible. Do not mistake successful CI execution for a quarantined-download Gatekeeper or SmartScreen approval.
- The smoke tests exercise help, subcommands and structured bad-input handling. They do not connect to a community or use a real Nostr key. An authenticated relay test is explicitly **not performed**.
- This project pins the source commit, Rust toolchain, Cargo lockfile and Linux base-image digest. Hosted runner images and OS package repositories can change: this is a traceable build recipe, **not a claim of bit-for-bit reproducibility**.

## Source and version policy

[`upstream.json`](upstream.json) is the single source pin. The first build uses upstream `desktop-v0.5.23`.
Our release version `0.5.23-1` means upstream release 0.5.23, packaging revision 1. It is not the upstream Cargo package's version string.

The workflow runs on pushes to `main`, pull requests and **Run workflow** in GitHub Actions.
**There is currently no periodic upstream watcher and no automatic release publication.** Those are deliberately deferred until the basic cross-platform build is established.
A maintainer updates the pin and publishes only a verified complete `release-bundle` artifact. Existing published assets must not be overwritten.

## Build it yourself

Prerequisites: Git, Python 3.11+, and either Docker Engine with Buildx (Linux) or Rustup plus the platform's native development tools (macOS/Windows).
Use a machine matching the requested OS and architecture; the scripts reject unintended cross-builds.

```sh
git clone https://github.com/frankhommers/buzz-cli-builds.git
cd buzz-cli-builds
python3 -m unittest discover -s tests -v
python3 scripts/build.py --target x86_64-unknown-linux-musl --output dist/linux
```

Select another target from the table as appropriate. Linux uses the pinned multi-architecture Rust/Alpine image; macOS/Windows use native builds.
On Windows, use a terminal with Python, Git, Rustup and the MSVC build tools available. The hosted Windows runner provides these prerequisites.
Never give the build your Buzz secrets. Linux builders receive no host credentials, Docker socket, or source checkout mounts.

To validate and assemble all five downloaded target artifacts without publishing:

```sh
python3 scripts/release.py --input dist/inputs --output dist/release --recipe-commit <exact-build-recipe-commit>
```

The aggregator rejects missing/duplicate targets, mismatching source or recipe commits, failed/skipped tests, malformed receipts, bad checksums, unsafe archive members and unexpected files.
All GitHub build jobs have read-only repository permissions, and checkout credentials are not persisted. Pull requests cannot publish releases.

## License

Build scripts and documentation: [Apache-2.0](LICENSE). Upstream Buzz: Apache-2.0, copyright its contributors.
Dependencies retain their own licenses; their bundled notices are not relicensed by this repository.
