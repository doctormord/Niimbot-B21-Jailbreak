#!/usr/bin/env python3
"""Patch official NIIMBOT B21 Pro firmware so no-name (non-RFID) rolls print at full darkness.

Usage:
    python3 b21patch.py B21_Pro_3.10.bin                  # writes B21_Pro_3.10_noprime10_crcfix.bin
    python3 b21patch.py B21_Pro_3.10.bin -o out.bin --density 0x14
    python3 b21patch.py B21_Pro_3.10.bin --verify         # also disassembles the patch (needs capstone)

Tested on 3.09, 3.10, 3.11, 3.12 and 3.13. Standard library only.

What it does (addresses are for 3.10, other versions are found by byte signature):

1. Always-valid: the RFID handler's success check is forced, so the roll
   normalizer runs for every roll and sets roll+9 = 1 ("label valid").
     3.09-3.11: cbz r5, <success>   ->  b <success>
     3.12-3.13: cbnz r5, <fail>     ->  nop
2. Default density: the call at the end of the normalizer is redirected
   into a code cave that sets roll+0x67 = density if it is still 0, so a roll
   that was never read from a tag still gets a sane heat value. Real tags keep
   their own density.
3. CRC-neutral: the header is left byte-identical (the printer checks a header
   hash that covers the CRC field), and 4 bytes in the unused reserved vector
   slot at 0x0800001C are solved so that crc32(body) equals the original CRC.
"""
import argparse
import struct
import sys
import zlib

HEADER = 0x1C
BASE = 0x08000000
COMPENSATE_AT = 0x1C          # reserved Cortex-M vector, never executed
CAVE_MIN = 24


def find_one(body, sig, what):
    i = body.find(sig)
    if i < 0 or body.find(sig, i + 1) >= 0:
        sys.exit(f"{what}: signature {'not found' if i < 0 else 'not unique'} - unsupported firmware?")
    return i


def bl_target(body, at):
    """Decode a 32-bit Thumb BL/B.W at body offset `at`, return target body offset."""
    hi, lo = struct.unpack_from("<HH", body, at)
    s = (hi >> 10) & 1
    imm10 = hi & 0x3FF
    j1, j2 = (lo >> 13) & 1, (lo >> 11) & 1
    imm11 = lo & 0x7FF
    i1, i2 = 1 - (j1 ^ s), 1 - (j2 ^ s)
    off = (s << 24) | (i1 << 23) | (i2 << 22) | (imm10 << 12) | (imm11 << 1)
    if s:
        off -= 1 << 25
    return at + 4 + off


def enc_branch32(at, target, link):
    """Encode Thumb-2 B.W (T4) or BL (T1) from body offset `at` to `target`."""
    off = target - (at + 4)
    if not -(1 << 24) <= off < (1 << 24) or off & 1:
        raise ValueError("branch out of range")
    off &= (1 << 25) - 1
    s = (off >> 24) & 1
    i1, i2 = (off >> 23) & 1, (off >> 22) & 1
    j1, j2 = 1 - (i1 ^ s), 1 - (i2 ^ s)
    hi = 0xF000 | (s << 10) | ((off >> 12) & 0x3FF)
    lo = (0xD000 if link else 0x9000) | (j1 << 13) | (j2 << 11) | ((off >> 1) & 0x7FF)
    return struct.pack("<HH", hi, lo)


def enc_b16(at, target):
    off = target - (at + 4)
    assert -2048 <= off < 2048 and not off & 1
    return struct.pack("<H", 0xE000 | ((off >> 1) & 0x7FF))


def cbz_target(body, at):
    hw = struct.unpack_from("<H", body, at)[0]
    assert hw & 0xF500 == 0xB100, "not cbz/cbnz"
    imm = (((hw >> 9) & 1) << 6) | (((hw >> 3) & 0x1F) << 1)
    return at + 4 + imm


def crc_compensate(body, at, want):
    """Choose body[at:at+4] so that crc32(body) == want. CRC32 is affine over GF(2)."""
    body[at:at + 4] = b"\0\0\0\0"
    base = zlib.crc32(body)
    cols = []
    for bit in range(32):
        body[at + bit // 8] = 1 << (bit % 8)
        cols.append(zlib.crc32(body) ^ base)
        body[at + bit // 8] = 0
    # Solve sum(x_i * cols[i]) == want ^ base by Gaussian elimination.
    rows = [(cols[i], 1 << i) for i in range(32)]
    pivots = {}
    for v, m in rows:
        for p in sorted(pivots, reverse=True):
            if v >> p & 1:
                v ^= pivots[p][0]
                m ^= pivots[p][1]
        if v:
            pivots[v.bit_length() - 1] = (v, m)
    target, sol = want ^ base, 0
    for p in sorted(pivots, reverse=True):
        if target >> p & 1:
            target ^= pivots[p][0]
            sol ^= pivots[p][1]
    if target:
        sys.exit("CRC compensation failed")
    body[at:at + 4] = sol.to_bytes(4, "little")
    assert zlib.crc32(body) == want


def patch(fw, density):
    hdr, body = fw[:HEADER], bytearray(fw[HEADER:])
    if struct.unpack_from("<I", hdr, 4)[0] != len(body):
        sys.exit("header length field does not match - not a B21 Pro image?")
    crc = struct.unpack_from("<I", hdr, 8)[0]
    if zlib.crc32(body) != crc:
        sys.exit("body CRC does not match header - already patched or corrupt?")
    if body[COMPENSATE_AT:COMPENSATE_AT + 4] != b"\0\0\0\0":
        sys.exit("reserved vector slot is not empty")

    # 1. always-valid gate
    a = body.find(bytes.fromhex("b0785db110b10aa0"))
    if a >= 0 and body.find(bytes.fromhex("b0785db110b10aa0"), a + 1) < 0:
        gate = a + 2                                     # cbz r5, <success>
        body[gate:gate + 2] = enc_b16(gate, cbz_target(body, gate))
        form = "cbz r5 -> b"
    else:
        gate = find_one(body, bytes.fromhex("0846f8f77ef9adb99df80020"), "always-valid gate") + 6
        body[gate:gate + 2] = bytes.fromhex("00bf")    # cbnz r5, <fail> -> nop
        form = "cbnz r5 -> nop"

    # 2. detour from the normalizer into a code cave
    det = find_one(body, bytes.fromhex("94f850008871"), "normalizer") + 6
    if body[det + 4:det + 8] != bytes.fromhex("94f86e00"):
        sys.exit("normalizer: unexpected code after call")
    callee = bl_target(body, det)
    i = 0
    while True:
        cave = body.find(b"\0" * CAVE_MIN, i)
        if cave < 0:
            sys.exit("no code cave found")
        if cave % 4 == 0:
            break
        i = cave + 1
    code = (bytes.fromhex("94f86700")                    # ldrb.w r0, [r4, #0x67]
            + bytes.fromhex("10b9")                      # cbnz   r0, +4  (skip if tag set it)
            + bytes([density, 0x20])                     # movs   r0, #density
            + bytes.fromhex("84f86700"))                 # strb.w r0, [r4, #0x67]
    code += enc_branch32(cave + len(code), callee, link=True)        # bl  <original callee>
    code += enc_branch32(cave + len(code), det + 4, link=False)      # b.w back
    body[cave:cave + len(code)] = code
    body[det:det + 4] = enc_branch32(det, cave, link=False)

    # 3. keep crc32(body) identical so the untouched header stays valid
    crc_compensate(body, COMPENSATE_AT, crc)
    info = dict(gate=gate, form=form, detour=det, cave=cave, cave_len=len(code), callee=callee)
    return hdr + bytes(body), info


def disasm(body, at, n):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    for ins in md.disasm(bytes(body[at:at + n]), BASE + at):
        print(f"    {ins.address:08x}  {ins.bytes.hex():<10} {ins.mnemonic} {ins.op_str}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("firmware", help="official B21_Pro_<ver>.bin")
    ap.add_argument("-o", "--output")
    ap.add_argument("--density", type=lambda s: int(s, 0), default=0x10,
                    help="default density for rolls without a tag (default 0x10, 1..255)")
    ap.add_argument("--verify", action="store_true", help="disassemble patched code (needs capstone)")
    a = ap.parse_args()
    if not 1 <= a.density <= 255:
        sys.exit("density must be 1..255")

    fw = open(a.firmware, "rb").read()
    out, info = patch(fw, a.density)
    name = a.output or a.firmware.rsplit(".", 1)[0] + f"_noprime{a.density:x}_crcfix.bin"
    open(name, "wb").write(out)

    print(f"gate    {BASE + info['gate']:08x}  {info['form']}")
    print(f"detour  {BASE + info['detour']:08x}  bl {BASE + info['callee']:08x} -> b.w cave")
    print(f"cave    {BASE + info['cave']:08x}  {info['cave_len']} bytes, default density {a.density:#x}")
    print(f"crc32   {zlib.crc32(out[HEADER:]):08x} (unchanged), header unchanged")
    print(f"wrote   {name}")
    if a.verify:
        body = out[HEADER:]
        for label, at, n in (("gate", info["gate"], 2), ("detour", info["detour"], 4),
                             ("cave", info["cave"], info["cave_len"])):
            print(f"  {label}:")
            disasm(body, at, n)


if __name__ == "__main__":
    main()
