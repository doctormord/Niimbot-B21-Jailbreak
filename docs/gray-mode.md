# B21 Pro gray mode: how it works and which variants exist

Patched NIIMBOT B21 Pro firmware images for **better 16-level grayscale**, built on top of the
`noprime10` full-heat patch. Everything here keeps the 28-byte header byte-identical and restores
`crc32(body) == header[0x08]` by solving the reserved Cortex-M vector word at `0x0800001C`
(GF(2) linear system), so the printer accepts the update. **None of these is a NIIMBOT release.**

The official images are not part of this repository (download them yourself, see README).
`Niimbot-B21-Jailbreak` repo (`tools/b21patch.py`): they make non-RFID / unknown rolls print at
full heat. Everything else adds a gray-heat change on top of that base.

Current best for photos (2026-10-27): **`*_graycum_load200_crcfix.bin`**.

## How grayscale actually works (reverse-engineered, emulation-confirmed)

- Each gray level L has a per-level **increment** `c_L`, computed in the setup function
  (~`0x0800b8a4` on 3.10) and stored at `0x200319b0 + 0x4c + L*0xde` (one 16-bit slot per level).
  The firmware adds temperature/voltage compensation on top.
- The splitter (`0x8012058`) is **thermometric**, not one-hot: a dot at level L is written into
  **all planes 0..L**. The strobe ISR fires each plane k once for `c_k` (+ corrections).
- So the darkness of a level-L dot ∝ **sum(c_0..c_L)** — cumulative. To get a linear darkness
  ramp the increments must be near-constant (hence graycum: a big `c_1`, then flat `c_2..c_14`).
- **Load term** (`0x801222c`, gray only): each plane's strobe time also gets
  `popcount(plane) × k0 × factor[level]` added (current/voltage-sag compensation). `k0 = 0.32`
  plus per-level factors `0.20 0.19 0.18 0.17 0.16 0.15 0.14`, stored as 8 doubles at a flash pool
  (found by byte signature). Full row (576 dots) adds ~25–36 ticks per plane at ×1. Paper tests
  showed this **under-compensates** (dense fields print lighter); ×1.5–×2 looks right.
- Per-plane strobe time is clamped to < 2000 (`0x8014c84`); the largest is L15 (~260 at load×2),
  far under the cap. 1-bit mode uses a separate heat path and is untouched by all of this.

## Naming

`B21_Pro_<ver>_noprime10[_<gray>][_<load>]_crcfix.bin`

- `<ver>` = 3.09 / 3.10 / 3.11 / 3.12 / 3.13.
- no `<gray>` → just the noprime base.
- `<gray>`:
  - `grayspread` — first attempt, c_L ramp 50..190 (early 3.10 one patched only rev-A; others both rev sets).
  - `grayauto` — two-profile via code caves (RFID roll → stock curve, no-tag → spread). **The first
    3.10 grayauto had a bug (wrote deltas to the wrong struct); superseded.**
  - `grayauto62` / `grayspread62` — narrower ramp c_L L1..L14 = 62..123, L15 untouched. 3.09–3.11
    two-profile, 3.12/3.13 always-spread (no cave room there, different read path).
  - `graycum` — **cumulative-aware curve**: increments c_1 = 160, c_2..c_14 = 34, c_15 untouched
    (rev A 190 / rev B 180). Gives a linear darkness ramp (sums 161,195,…,603, then full black).
    1-bit untouched. 3.09–3.11 two-profile, 3.12/3.13 always-spread.
- `<load>` (on graycum only) scales the load-term compensation:
  - `load0` — factors = 0 → **no** load compensation (net ×0).
  - `load50` — all 8 factors ×0.5 → **net ×0.25** (k0 is one of the 8 and multiplies again, so it squares).
  - (no suffix) — net ×1 (stock load term).
  - `load150` — only the 7 per-level factors ×1.5, k0 kept → **net ×1.5**.
  - `load200` — only the 7 per-level factors ×2, k0 kept → **net ×2**. ← current best.

> Note on scaling: scaling *all 8* factors by N gives net ×N² (k0 double-counts). `load0`/`load50`
> did that (×0 / ×0.25); `load150`/`load200` scale only the per-level 7 to hit a clean net ×1.5/×2.

## Build / patch mechanics

- Header (`fw[:0x1C]`) is never touched. `fw[0x1C:]` loads at `0x08000000`.
- Gray curve: patch the per-level `adds #imm8` (and the shared `add.w` for L1) in **both** board-rev
  constant sets; L15 and the 1-bit value are left alone.
- Two-profile (`grayauto`/`graycum` on 3.09–3.11): two code caves in zero padding — one clears the
  roll type on a failed RFID read, one adds a rev-dependent per-level delta at the setup return.
- Load term: overwrite the 8 factor doubles in the flash pool.
- CRC: zero the word at file `0x38`, solve 4 bytes so `crc32(fw[0x1C:]) == header[0x08]`.

## Flashing

niim.blue firmware updater or the B21Pro app debug console. Pick the version matching the file.
Keep the printer charged and connected. Back to stock = flash any official image. See the
Jailbreak repo for SWD/UART recovery.

## Backlog / open

- Pick the final load factor from paper (load200 leading).
- Re-measure the per-paper tone curve (full-width band chart averages out the load effect) and feed
  the in-app calibration; a dithered photo has lower per-plane load than a solid band, so a
  band-measured curve over-estimates photo darkness until the load term is tuned.
- Port the two-profile (cave) path to 3.12/3.13 (needs a split cave + read-path check).

## Browser editor

`b21_gray_editor.html` — open it in a browser (double-click; works offline, nothing is uploaded).
Load an official or `*_noprime10_crcfix.bin` image, pick a profile and load factor, watch the increment
bars and the cumulative darkness lines (thin vs full-row), and download a flashable image (header kept,
CRC re-solved). For 3.09–3.11 it builds the **RFID-aware two-profile** firmware (installs the two caves,
fills the per-level delta table from your curve); for 3.12/3.13 it writes an always-spread curve. L15 and
the 1-bit path are left alone. Presets: `stock`, `62er`, `L50 / L100 / L150 / L200` (graycum curve at load
net ×0.5 / ×1 / ×1.5 / ×2). Default = **L200**. Verified bit-identical to the reference
`graycum_load200` build. The Python scripts remain the reference implementation.
