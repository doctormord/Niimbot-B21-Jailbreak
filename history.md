# B21 Pro Firmware Patch — Work History

```yaml
project: niimbot-b21pro-fullheat-no-rfid
device: NIIMBOT B21 Pro (STM32F103-compatible, ARM Cortex-M, Thumb, LE, load 0x08000000)
goal: print on no-name (no-RFID) label rolls at full darkness
date: 2026-10-24
status: SOLVED and confirmed on device
two_problems:
  A: make a modified firmware pass the printer's integrity/CRC checks (flashable)
  B: defeat the heat throttle that triggers when no valid RFID tag is present
outcome_files: firmware/B21_Pro_{3.09,3.10,3.11,3.12,3.13}_noprime10_crcfix.bin
```

This is a chronological, human- and machine-readable log of what was done and why.
Addresses are for firmware **3.10** unless noted; other versions share the structure at shifted
addresses (located by byte-signature search and verified with capstone).

---

## Phase 0 — Starting point (prior session)
- Had original firmwares `firmware/original/B21_Pro_3.09..3.13.bin` and earlier patched builds
  (`_fullheat`, `_fullheat_keephdr`).
- Prior notes: `FIRMWARE_ANALYSE.md` (assumed throttle = `movs r7,#0x1e`), `RECOVERY.md` (flash safety,
  staging + CRC-on-commit, STM32F1, SWD/ROM-bootloader recovery).
- Symptom: originals flash fine; patched `keephdr` flashes to the end then fails on body CRC;
  patched (recomputed CRC) is rejected early.

## Phase 1 — Problem A: make patched firmware flashable (CRC)
1. Dumped and decoded the 28-byte header of all 5 originals. Fields (LE):
   `[0:4]`=hash(custom), `[4:8]`=body length (file−28), `[8:12]`=`crc32(body)` (zlib),
   `[12:20]`=0, `[20:24]`=version, `[24:28]`=hash(custom).
2. Brute-forced a full CRC catalog (all standard poly/init/reflect variants, STM32 hardware-CRC,
   self-referential) over every header slice → **no match** for `[0:4]`/`[24:28]`. They use a
   custom algorithm; not worth cracking.
3. Diffed original vs `_fullheat`/`_keephdr` → confirmed model: printer checks a header integrity
   early (covers `[8:12]`) and `crc32(body)==[8:12]` at the end.
4. **Insight:** CRC32 is linear → keep the header byte-identical, apply the body patch, then adjust
   4 free body bytes so `crc32(body)` returns to the original `[8:12]`. Free bytes = reserved
   Cortex-M vector slot `0x0800001C` (never executed, still covered by the CRC). Solved via GF(2)
   linear algebra (`build2.solve`).
5. Built `_crcfix` images; **confirmed on device: they flash through and boot.** (Problem A solved.)

## Phase 2 — Problem B: first (wrong) heat lever
- Patched the old "throttle" `movs r7,#0x1e` → higher values, CRC-neutral.
- Device results: r7=0x1e faint, 0x7f/0xfe/0xff blank, 0x60(96) same faint as 0x1e, 0x70(112) blank.
  **Non-monotonic → r7 is NOT the darkness lever** (just an overcurrent guard). Dead end.

## Phase 3 — Build a real RE environment (Ghidra)
- Installed JDK 21 (`brew install openjdk@21`).
- Downloaded Ghidra 12.1.4 PUBLIC; the public release ships decompiler binaries only for
  linux_x86_64 / win_x86_64 → **built the decompiler from source for macOS/arm64**
  (`make ghidra_opt ARCH_TYPE="-arch arm64" ADDITIONAL_FLAGS="-w"`), installed to
  `os/mac_arm_64/decompile`. (Details in `rea.md`.)
- Imported the header-stripped body as raw `ARM:LE:32:Cortex` at base `0x08000000`; drove it
  headless with a decompile script. capstone used for Thumb disassembly + patch verification.

## Phase 4 — Mapping the real energy/strobe path
- Real per-roll energy is computed in `FUN_08013b34` (big command dispatcher), case 1:
  `if (roll[+9]==1) energy = base * density(roll[+0x67]/+0x77/+0x6e...) * 11.81 → strobe[+0x1c0]`
  else `strobe[+0x1c0]=0`. Roll struct `0x2000176c`, strobe struct `0x2000c5e8`, base `0x20000610`.
- Consumer `FUN_080146c8`: if `strobe[+0x1c0]==0` **or** `strobe[+0x210]+[+0x1c0] > cap[+0x1ba]`
  → minimal fallback `r0=0xe` (this IS the stock faint output). The cap is derived from `base`,
  not from the tag.
- Tried: inject energy into `+0x1c0`, raise the `0xe` floor, inject density (fakecal, force-valid +
  constants). Device: **blank / 50% hang (head-protection) / non-monotonic.** The strobe timing is
  tightly interlocked — single injected values break it. Dead ends.

## Phase 5 — Talk to the printer over USB
- Printer = `/dev/cu.usbmodem101`, NIIMBOT `0x55` protocol. Wrote a pyserial client.
- `GET_RFID (0x1A)` returns tag identity (UUID/barcode/serial/type); `GET_INFO (0x40)` returns
  device info. **No memory-read command exists** → the real heat calibration cannot be read without
  SWD/ST-Link.
- Confirmed via decompile: density `roll[+0x67]` is read straight from the tag
  (`FUN_080165d8` copies 0x6c tag bytes into the roll struct; density = tag offset 0x53). **Not**
  persisted independently.

## Phase 6 — The breakthrough (user's "reroute the whole no-tag path")
- RFID handler `FUN_0800e004`: loops a read `FUN_080165d8`; on success calls normalizer
  `FUN_0800df8c`, which sets **`roll[+9]=1`** and defaults (`+0x6e/+0x6d/+0x6c ≥ 1`).
- Key facts: on a failed (no-tag) read, `FUN_080165d8` does **not** clear `roll[+0x67]`; and forcing
  the success path = exactly what a real tag does (a safe, well-exercised path).
- **`alwaysvalid` patch:** force the handler's success gate to always take success
  (3.09/10/11: `cbz r5,success`→`b`; 3.12/13: `cbnz r5,fail`→`NOP`).
- Test (prime with a real RFID roll once, then swap to a no-name roll **without power-cycle**):
  **no-name printed DARK.** (Density lived in RAM from the prior read.) First success.

## Phase 7 — "No-prime" final version
- To avoid any priming and survive power-off: a code-cave detour from `FUN_0800df8c` that does
  `if (roll[+0x67]==0) roll[+0x67]=0x10` (default density 16). Real tags keep their own density
  (skip when nonzero). Cave placed in a zero-padding run; branches hand-encoded and
  **capstone-verified**; CRC-neutralized.
- Energy now runs through the **real** firmware formula (cap-bounded) → consistent timing, no hang.
- Device: **no-name prints dark with no tag ever, default `0x10` confirmed good.** (Problem B solved.)

## Phase 8 — Roll-out
- Applied `alwaysvalid` + `no-prime(0x10)` to **3.09/3.10/3.11/3.12/3.13**, both always-valid code
  forms handled, each located by signature and verified (header unchanged, `crc32(body)==[8:12]`).
- Deliverables: `firmware/B21_Pro_<ver>_noprime10_crcfix.bin`. Full description in `README.md`.

---

## Timeline summary (machine-readable)
```yaml
phases:
  - id: A-crc;        result: solved;   note: "CRC-neutral trick (4 reserved-vector bytes)"
  - id: B-r7;         result: deadend;  note: "movs r7,#0x1e not the darkness lever"
  - id: env-ghidra;   result: ok;       note: "JDK21 + Ghidra 12.1.4, decompiler built for arm64"
  - id: map-energy;   result: ok;       note: "FUN_08013b34 case1; strobe 0x2000c5e8; cap 0x1ba"
  - id: inject-heat;  result: deadend;  note: "energy/floor/density injection -> blank/50%-hang"
  - id: usb-proto;    result: partial;  note: "0x55 protocol; GET_RFID 0x1A; no memory-read"
  - id: alwaysvalid;  result: solved;   note: "force RFID success -> roll+9=1; prime-then-swap dark"
  - id: no-prime;     result: solved;   note: "cave sets roll+0x67=0x10 default; no priming needed"
  - id: rollout;      result: solved;   note: "3.09-3.13 built + verified"
confirmed_on_device: [A-crc, alwaysvalid, no-prime(3.10)]
```
