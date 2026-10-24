# Reverse-Engineering Environment (REA) — Setup & Build Notes

```yaml
purpose: tools + exact steps used to analyze and patch the B21 Pro firmware
host: macOS (Darwin 25.5.0), Apple Silicon (arm64)
key_outcome: a working Ghidra decompiler for mac_arm_64 (built from source) + Thumb tooling
note: the analysis tools live in an EPHEMERAL per-session scratchpad; this file lets you rebuild them
```

All commands assume macOS + Homebrew. The scratchpad path below was session-specific and may not
persist; recreate a working dir and adjust `SCRATCH` as needed.

```sh
SCRATCH=/tmp/b21_rea        # any working directory
mkdir -p "$SCRATCH" && cd "$SCRATCH"
```

---

## 1. Toolchain (already present on the host)
- Apple clang **14.0.3** (`clang --version`), plus `make`, `bison`, `flex` (Xcode Command Line Tools).
- Homebrew.
These were pre-installed; `xcode-select --install` provides them if missing.

## 2. Java (for Ghidra)
```sh
brew install openjdk@21        # installed openjdk 21.0.13.0.2
export JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
"$JAVA_HOME/bin/java" -version  # verify
```

## 3. Ghidra 12.1.4 (download + extract)
```sh
mkdir -p "$SCRATCH/ghidra" && cd "$SCRATCH/ghidra"
curl -L -o ghidra.zip \
  "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_12.1.4_build/ghidra_12.1.4_PUBLIC_20260921.zip"
unzip -q ghidra.zip
GHIDRA="$SCRATCH/ghidra/ghidra_12.1.4_PUBLIC"
```
Version 12.1.4 was chosen to match the earlier analysis. The REA MCP server also expects a
`GHIDRA_INSTALL_DIR` pointing at a 12.1.x release if you use REA instead of headless.

## 4. Build the Ghidra decompiler for macOS/arm64 (REQUIRED)
The public Ghidra release ships the native decompiler **only** for `linux_x86_64` and `win_x86_64`.
On Apple Silicon you must build it from the bundled C++ source.

```sh
CPP="$GHIDRA/Ghidra/Features/Decompiler/src/decompile/cpp"
cd "$CPP"
make ghidra_opt ARCH_TYPE="-arch arm64" ADDITIONAL_FLAGS="-w"
```

Why the overrides (pitfalls that cost us two failed builds):
1. The Makefile's `Darwin` branch hardcodes `ARCH_TYPE=-arch x86_64` and
   `ADDITIONAL_FLAGS=-mmacosx-version-min=10.6 -w`. On an arm64 host with a modern SDK this makes
   `#include <cstdint>` fail ("file not found"). → override `ARCH_TYPE="-arch arm64"` and drop the
   10.6 min-version (`ADDITIONAL_FLAGS="-w"`).
2. `ghidra_opt` must be the **sole** `make` goal. The Makefile picks dependency sets via
   `ifeq ($(MAKECMDGOALS),ghidra_opt)`, which only matches a single goal. Building
   `ghidra_opt sleigh_opt` together falls back to `com_*` objects that `#include <bfd.h>` (binutils,
   absent on macOS) → build fails. Build each target separately. (`sleigh` is not needed here;
   Ghidra compiles `.sla` via Java.)
- `CXX` in the Makefile is `g++ -std=c++11` (that's Apple clang on macOS). No change needed.

Install the built binary where Ghidra looks for it:
```sh
mkdir -p "$GHIDRA/Ghidra/Features/Decompiler/os/mac_arm_64"
cp "$CPP/ghidra_opt" "$GHIDRA/Ghidra/Features/Decompiler/os/mac_arm_64/decompile"
chmod +x "$GHIDRA/Ghidra/Features/Decompiler/os/mac_arm_64/decompile"
file "$GHIDRA/Ghidra/Features/Decompiler/os/mac_arm_64/decompile"   # => Mach-O 64-bit arm64
```

## 5. Python tooling (Thumb disassembly + serial)
```sh
cd "$SCRATCH"
python3 -m venv venv
./venv/bin/pip install capstone pyserial
# installed: capstone 5.0.7, pyserial 3.5
```
Notes:
- `keystone-engine` (assembler) **could not** be installed (Python 3.15 removed `distutils`;
  `ModuleNotFoundError: No module named 'distutils'`). Not required — the few Thumb branches
  (B.W/BL/CBNZ/narrow-B) were hand-encoded and then **verified by disassembling with capstone**.
- jadx was installed (`brew install jadx`) but **not** ultimately used; the APK was inspected by
  unzipping + `strings`. (REA's Android provider needs a caller-supplied `jadx-headless-mcp` JAR via
  `REA_JADX_MCP_JAR`, which was not set up.)

## 6. Loading the firmware in Ghidra (headless)
The .bin has a 28-byte header; strip it so the body maps at `0x08000000`:
```sh
tail -c +29 firmware/original/B21_Pro_3.10.bin > "$SCRATCH/fw/B21_3.10_body.bin"
```
Import + auto-analyze (raw Cortex-M Thumb):
```sh
"$GHIDRA/support/analyzeHeadless" "$SCRATCH/gproj" b21 \
  -import "$SCRATCH/fw/B21_3.10_body.bin" \
  -processor ARM:LE:32:Cortex -loader BinaryLoader -loader-baseAddr 0x08000000
```
Re-run scripts against the saved project without re-analyzing:
```sh
"$GHIDRA/support/analyzeHeadless" "$SCRATCH/gproj" b21 \
  -process B21_3.10_body.bin -noanalysis \
  -scriptPath "$SCRATCH/gscripts" -postScript DecompAt.java 0x08013b34 0x0800e004
```
Scripts used (in `$SCRATCH/gscripts/`):
- `DecompAt.java` — decompile the function containing each given address (creates the function and
  sets Thumb/TMode if Ghidra missed it); prints pseudo-C.
- `Callers.java` — list references/callers of the function containing an address.

## 7. The printer over USB (optional, for inspection)
- Device node: `/dev/cu.usbmodem101`. Protocol: NIIMBOT `0x55 0x55 CMD LEN DATA XOR AA AA`.
- Useful read-only requests: `GET_RFID = 0x1A` (tag identity), `GET_INFO = 0x40` (device info).
- There is **no** memory-read/debug-dump command in the protocol; heat calibration is not readable
  this way (would need SWD/ST-Link). The port is exclusive — disconnect niimblue before opening it.

## 8. Patch/build pipeline (Python, in `$SCRATCH`)
> These scratch scripts were not kept. Their logic is consolidated in `tools/b21patch.py`, which
> reproduces the device-tested `_noprime10_crcfix` images for 3.09–3.13 byte for byte.

- `build2.py` — core CRC-neutral patcher: applies byte patches, then solves 4 bytes at body
  `0x1c` (reserved vector) over GF(2) so `crc32(body)` equals the original `hdr[8:12]`; header stays
  byte-identical (keeps the printer's early integrity check valid).
- `build_all.py` / `build_noprime.py` — locate the two patch sites per version by byte-signature,
  hand-encode the Thumb branches, lay down a code cave, verify every instruction with capstone,
  then CRC-neutralize. Output: `firmware/B21_Pro_<ver>_noprime10_crcfix.bin`.
- `fwdisasm.py` — small capstone Thumb disassembler (`<body.bin> <vaddr> <len>`), used to verify
  every patch before building.

## Versions (summary, machine-readable)
```yaml
macos: Darwin 25.5.0 (arm64)
clang: Apple clang 14.0.3
jdk: openjdk@21 (21.0.13.0.2)  # JAVA_HOME=/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home
ghidra: 12.1.4 PUBLIC (ghidra_12.1.4_PUBLIC_20260921.zip)
ghidra_decompiler: built from source for mac_arm_64 (make ghidra_opt ARCH_TYPE="-arch arm64" ADDITIONAL_FLAGS="-w")
python_pkgs: {capstone: 5.0.7, pyserial: "3.5", keystone-engine: FAILED(distutils-missing, not needed)}
jadx: installed via brew but unused
firmware_target: ARM:LE:32:Cortex, base 0x08000000, 28-byte header stripped for analysis
```
