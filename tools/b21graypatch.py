#!/usr/bin/env python3
"""Spread the B21 Pro's 16 gray heat levels so all shades fall in the usable range.

The firmware maps each 4-bit gray level to a thermal strobe time with a fixed set
of per-level constants, computed in the setup function (around 0x0800b8c0 on 3.10)
and stored into the strobe structure. The stock curve is bunched up: levels 1-14
use only ~10-40 % of a full dot and level 15 jumps straight to the 1-bit value.
On third-party paper, whose colouring threshold is higher, the low levels come out
blank, so only ~6 of the 16 shades actually print.

This patcher rewrites those constants to a smooth ramp, so all 16 levels land in
the paper's working range. The firmware keeps applying its own temperature and
voltage compensation on top; only the per-level base changes.

    stock  (3.10, nominal 25 C / 7.8 V / density 3, timer ticks):
        22 22 27 27 28 34 40 45 52 57 64 64 74 84 204
    default ramp:
        52 63 74 84 92 102 112 123 132 143 151 162 168 179 204

Both the firmware's rev-A and rev-B constant sets are patched, so the result is the
same ramp regardless of which board revision the printer reports.

Usage:
    python3 b21graypatch.py B21_Pro_3.10_noprime10_crcfix.bin
        -> B21_Pro_3.10_noprime10_grayspread_crcfix.bin
    python3 b21graypatch.py B21_Pro_3.10.bin -o out.bin
    python3 b21graypatch.py ... --curve 50,61,72,82,92,102,112,123,132,143,151,162,168,179,190
    python3 b21graypatch.py ... --verify

Run it on an official image or on a *_noprime10_crcfix.bin (b21patch.py output);
it works on both. Tested on 3.09, 3.10, 3.11, 3.12 and 3.13.

Needs capstone (`pip install capstone`) to locate the constants; the CRC and
encoding machinery is reused from b21patch.py.
"""
import argparse
import sys
import zlib

try:
    import capstone
except ImportError:
    sys.exit("this patcher needs capstone: pip install capstone")

from b21patch import BASE, HEADER, COMPENSATE_AT, crc_compensate

# Heat-value slot of each gray level inside the strobe structure (stride 0xde).
# Level 0 (0x4c) is white and left alone; these are levels 1..15.
PLANE_OFFSETS = [0x12a, 0x208, 0x2e6, 0x3c4, 0x4a2, 0x580, 0x65e, 0x73c,
                 0x81a, 0x8f8, 0x9d6, 0xab4, 0xb92, 0xc70, 0xd4e]

# Default per-level base constant (c_L). Nominal pulse is c_L plus the firmware's
# small voltage term, i.e. roughly a linear 52..204 ticks across the 16 levels.
DEFAULT_CURVE = [50, 61, 72, 82, 92, 102, 112, 123, 132, 143, 151, 162, 168, 179, 190]


def disasm_all(body):
    md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
    md.detail = True
    md.skipdata = True
    return list(md.disasm(bytes(body), BASE))


def find_setup(ins):
    """Return (start, pop, nextfn) instruction indices of the gray heat setup
    function: the one that branches on the board revision (cmp #0x42) and stores
    the 15 per-level constants into the plane slots."""
    setup = None
    for k, i in enumerate(ins):
        if i.mnemonic == "cmp" and i.op_str.endswith("#0x42"):
            stores = sum(1 for j in ins[k:k + 240]
                         if j.mnemonic.startswith("strh")
                         and any(("#0x%x]" % p) in j.op_str for p in PLANE_OFFSETS))
            if stores >= 12:
                setup = k            # the setup fn is the later of the two cmp #0x42
    if setup is None:
        sys.exit("gray heat setup function not found - unsupported firmware?")
    start = next(j for j in range(setup, setup - 160, -1)
                 if ins[j].mnemonic == "push.w" and "lr}" in ins[j].op_str)
    pop = next(j for j in range(setup, setup + 450)
               if ins[j].mnemonic == "pop.w" and "pc}" in ins[j].op_str and "r8" in ins[j].op_str)
    # The rev-B constant set lives after the pop, up to the next function.
    nextfn = next((j for j in range(pop + 1, pop + 400)
                   if ins[j].mnemonic == "push.w" and "lr}" in ins[j].op_str), pop + 300)
    return start, pop, nextfn


def find_constants(ins, start, nextfn):
    """Map every per-level constant add to (body_offset_of_imm8, level). Both the
    rev-A and rev-B sets are returned; shared levels (1 and sometimes 15) appear
    once per occurrence."""
    out = {}
    for j in range(start, nextfn):
        i = ins[j]
        if not i.mnemonic.startswith("strh"):
            continue
        level = next((L for L, p in enumerate(PLANE_OFFSETS, 1) if ("#0x%x]" % p) in i.op_str), None)
        if level is None:
            continue
        rd = i.op_str.split(",")[0]
        for jj in range(j, max(start - 1, j - 12), -1):
            a = ins[jj]
            if a.mnemonic in ("adds", "add.w") and a.op_str.split(",")[0] == rd and "#" in a.op_str:
                body_off = a.address - BASE            # imm8 byte position
                if a.size == 4:                        # add.w rD, rD, #imm8 (T3): imm8 is byte 2
                    body_off += 2
                out[body_off] = level
                break
    if len(out) < 20:
        sys.exit("found only %d gray constants - unsupported firmware?" % len(out))
    return out


def patch(fw, curve):
    hdr, body = fw[:HEADER], bytearray(fw[HEADER:])
    import struct
    if struct.unpack_from("<I", hdr, 4)[0] != len(body):
        sys.exit("header length field does not match - not a B21 Pro image?")
    crc = struct.unpack_from("<I", hdr, 8)[0]
    if zlib.crc32(body) != crc:
        sys.exit("body CRC does not match header - corrupt image?")
    # The reserved vector slot at COMPENSATE_AT is the CRC fix-up word. On a stock
    # image it is zero; on a b21patch.py output it already holds noprime's value.
    # crc_compensate zeroes and re-solves it for all edits combined, so either is fine.

    ins = disasm_all(body)
    start, pop, nextfn = find_setup(ins)
    consts = find_constants(ins, start, nextfn)
    for body_off, level in consts.items():
        new = curve[level - 1]
        if not 0 <= new <= 255:
            sys.exit("curve value for level %d out of range: %d" % (level, new))
        body[body_off] = new

    crc_compensate(body, COMPENSATE_AT, crc)
    info = dict(setup=ins[start].address, count=len(consts),
                offsets=sorted(consts), levels=consts)
    return hdr + bytes(body), info


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("firmware", help="official B21_Pro_<ver>.bin or a *_noprime10_crcfix.bin")
    ap.add_argument("-o", "--output")
    ap.add_argument("--curve", help="15 comma-separated per-level base constants (levels 1..15)")
    ap.add_argument("--verify", action="store_true", help="disassemble the patched constants")
    a = ap.parse_args()

    if a.curve:
        curve = [int(x, 0) for x in a.curve.split(",")]
        if len(curve) != 15:
            sys.exit("--curve needs exactly 15 values (levels 1..15)")
    else:
        curve = DEFAULT_CURVE

    fw = open(a.firmware, "rb").read()
    out, info = patch(fw, curve)
    if a.output:
        name = a.output
    elif a.firmware.endswith("_crcfix.bin"):
        name = a.firmware[:-len("_crcfix.bin")] + "_grayspread_crcfix.bin"
    else:
        name = a.firmware.rsplit(".", 1)[0] + "_grayspread_crcfix.bin"
    open(name, "wb").write(out)

    print("setup   %08x  gray heat setup function" % info["setup"])
    print("curve   %s" % " ".join(str(c) for c in curve))
    print("patched %d constants (rev-A + rev-B)" % info["count"])
    print("crc32   %08x (unchanged), header unchanged" % (zlib.crc32(out[HEADER:]) & 0xffffffff))
    print("wrote   %s" % name)
    if a.verify:
        body = out[HEADER:]
        md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB | capstone.CS_MODE_MCLASS)
        for off in info["offsets"]:
            start = off - 2 if body[off + 1] & 0xf0 != 0x30 else off   # add.w vs adds
            ins = next(md.disasm(bytes(body[start:start + 4]), BASE + start))
            print("  L%-2d %08x  %s %s" % (info["levels"][off], ins.address, ins.mnemonic, ins.op_str))


if __name__ == "__main__":
    main()
