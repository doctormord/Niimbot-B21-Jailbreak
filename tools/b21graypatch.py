#!/usr/bin/env python3
"""Rework the B21 Pro's 16-level gray heat curve (and its load compensation).

How gray works in the firmware (3.09 - 3.13, found by disassembly + emulation):

* Every gray level L has an increment c_L that the setup function stores into the
  strobe structure (stride 0xde). Temperature and voltage terms are added on top.
* The row splitter is thermometric: a dot of level L is written into planes 0..L and
  every plane fires once for its c_k. Darkness of a level-L dot is therefore
  proportional to the SUM c_0..c_L, not to c_L. A linear grayscale needs a large c_1
  and flat increments after it.
* The strobe time of a plane also gets popcount(plane) * k0 * factor[level] added
  (current-sag compensation, 8 doubles in a flash pool). Paper tests show the stock
  value under-compensates; a net factor of about 2 balances dithered photos and bands.
* Level 15 and the 1-bit path have their own constants and are never touched.

Profiles (--profile):
    stock    the firmware's own curve (use with --load to change only the load term)
    graycum  c_1 = 160, c_2..c_14 = 34: linear darkness ramp (current best)
    ramp62   early ramp 62..123, kept for comparison
    custom   --curve with 14 values (levels 1..14)

Modes (--mode):
    spread   write the curve into both board-revision constant sets. Works on every
             version and on official as well as noprime images.
    auto     RFID-aware: a genuine gray roll keeps the stock curve, a roll without a
             readable tag gets the new one. Needs two code caves, so it is limited to
             3.09 - 3.11. Hook sites and free cave space are checked before writing;
             tested on printer with b21patch.py output (*_noprime10_crcfix.bin).
    The default is auto where possible, otherwise spread.

Usage:
    python3 b21graypatch.py B21_Pro_3.10_noprime10_crcfix.bin --load 2
    python3 b21graypatch.py B21_Pro_3.12.bin --mode spread --profile graycum --load 2
    python3 b21graypatch.py B21_Pro_3.13.bin --profile custom --curve 160,34,34,34,34,34,34,34,34,34,34,34,34,34

The header is never touched; the CRC is restored through the reserved vector word, as
in b21patch.py. Needs capstone (pip install capstone). Flash at your own risk; keep a
stock image at hand. No firmware image is part of this repository.
"""
import argparse
import struct
import sys
import zlib

try:
    import capstone
except ImportError:
    sys.exit("this patcher needs capstone: pip install capstone")

from b21patch import BASE, HEADER, COMPENSATE_AT, crc_compensate

# Heat-value slot of each gray level inside the strobe structure (stride 0xde).
PLANE_OFFSETS = [0x12a, 0x208, 0x2e6, 0x3c4, 0x4a2, 0x580, 0x65e, 0x73c,
                 0x81a, 0x8f8, 0x9d6, 0xab4, 0xb92, 0xc70, 0xd4e]

# Stock increments c_L for L1..L15 per board revision (identical in 3.09 - 3.13).
STOCK_A = [20, 20, 25, 25, 25, 30, 35, 40, 45, 50, 55, 55, 60, 70, 190]
STOCK_B = [20, 25, 30, 30, 30, 30, 40, 40, 50, 50, 60, 60, 60, 80, 180]

PROFILES = {
    "stock": STOCK_A[:14],
    "graycum": [160] + [34] * 13,
    "ramp62": [62, 67, 71, 76, 81, 85, 90, 95, 99, 104, 109, 113, 118, 123],
}

# Load term: k0 and the seven per-level factors, as doubles in a flash pool.
LOAD_K0 = 0.32
LOAD_FACTORS = [0.20, 0.19, 0.18, 0.17, 0.16, 0.15, 0.14]

# RFID-aware mode: cave bytes and hook sites per version, for noprime images only.
# cave1 clears the stale roll type before a tag read, cave2 adds per-level int16
# deltas (rev A table, then rev B table, at delta_at) at the end of the setup function.
AUTO = {
    "3.09": dict(
        size=122768,
        cave1_at=112856, cave1="0cb441f26c73c2f20003002283f850200cbcfaf7a3be",
        cave2_at=118908, cave2=(
            "41f26c73c2f2000393f85030022b1cd04cf2e851c2f200010978422902d1"
            "0ff24c0201e00ff2260241f6b010c2f203000121de264e434c36845b4d1e"
            "32f915502c44845301311029f3d3bde8f08f8c000e000900090009000400"
            "fffffafff5fff0ffebffebffe6ffdcff00008c0009000400040004000400fafffafff0fff0ffe6ffe6ffe6ffd2ff0000"),
        delta_at=78,
        tagcall_at=57456, tagcall_new="0df032fc", tagcall_old="08f0e0fa",
        pop_at=47922, pop_new="11f0a3ba", pop_old="bde8f08f"),
    "3.10": dict(
        size=122700,
        cave1_at=112792, cave1="0cb441f26c73c2f20003002283f850200cbcfaf7a3be",
        cave2_at=118844, cave2=(
            "41f26c73c2f2000393f85030022b1cd04cf2e851c2f200010978422902d1"
            "0ff24c0201e00ff2260241f6b010c2f203000121de264e434c36845b4d1e"
            "32f915502c44845301311029f3d3bde8f08f8c000e000900090009000400"
            "fffffafff5fff0ffebffebffe6ffdcff00008c0009000400040004000400fafffafff0fff0ffe6ffe6ffe6ffd2ff0000"),
        delta_at=78,
        tagcall_at=57392, tagcall_new="0df032fc", tagcall_old="08f0e0fa",
        pop_at=47858, pop_new="11f0a3ba", pop_old="bde8f08f"),
    "3.11": dict(
        size=122812,
        cave1_at=112892, cave1="0cb441f26c73c2f20003002283f850200cbcfaf747bf",
        cave2_at=118944, cave2=(
            "41f26c73c2f2000393f85030022b1cd04cf2e851c2f200010978422902d1"
            "0ff24c0201e00ff2260241f6b010c2f203000121de264e434c36845b4d1e"
            "32f915502c44845301311029f3d3bde8f08f8c000e000900090009000400"
            "fffffafff5fff0ffebffebffe6ffdcff00008c0009000400040004000400fafffafff0fff0ffe6ffe6ffe6ffd2ff0000"),
        delta_at=78,
        tagcall_at=57360, tagcall_new="0df074fc", tagcall_old="08f0c6fb",
        pop_at=48074, pop_new="11f069ba", pop_old="bde8f08f"),
}


def sig_load_block(body):
    """Offset of the 8 load-term doubles (stock values), or exit."""
    sig = struct.pack("<8d", LOAD_K0, *LOAD_FACTORS)
    hits = [i for i in range(0, len(body) - len(sig), 4) if body[i:i + len(sig)] == sig]
    if len(hits) != 1:
        sys.exit("load factor block not found (%d hits) - image already load-patched?" % len(hits))
    return hits[0]


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
    """Map every per-level constant add to {body_offset_of_imm8: level}. Both the
    rev-A and rev-B sets are returned."""
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
    if len(out) != 28:
        sys.exit("found %d gray constants, expected 28 - unsupported firmware?" % len(out))
    return out


def check_image(fw):
    hdr, body = fw[:HEADER], bytearray(fw[HEADER:])
    if struct.unpack_from("<I", hdr, 4)[0] != len(body):
        sys.exit("header length field does not match - not a B21 Pro image?")
    crc = struct.unpack_from("<I", hdr, 8)[0]
    if zlib.crc32(body) != crc:
        sys.exit("body CRC does not match header - corrupt image?")
    return hdr, body, crc


def patch(fw, curve, load, mode):
    hdr, body, crc = check_image(fw)
    ins = disasm_all(body)
    start, pop, nextfn = find_setup(ins)
    consts = find_constants(ins, start, nextfn)

    stock = curve == PROFILES["stock"]          # only the load term changes
    auto = AUTO.get(next((v for v, a in AUTO.items() if len(fw) == a["size"]), None)) \
        if mode in ("auto", "best") and not stock else None
    if mode == "auto" and auto is None and not stock:
        sys.exit("auto mode needs a 3.09 - 3.11 image (official or b21patch.py output)")
    if auto is not None:
        for at, old in ((auto["tagcall_at"], auto["tagcall_old"]), (auto["pop_at"], auto["pop_old"])):
            if body[at - HEADER:at - HEADER + 4].hex() != old:
                if mode == "auto":
                    sys.exit("hook site 0x%x does not match - unknown image" % at)
                auto = None
                break
        else:
            for at, key in ((auto["cave1_at"], "cave1"), (auto["cave2_at"], "cave2")):
                n = len(bytes.fromhex(auto[key]))
                if any(body[at - HEADER:at - HEADER + n]):
                    if mode == "auto":
                        sys.exit("cave space at 0x%x is not free" % at)
                    auto = None
                    break

    # offsets in file terms for the dicts above, in body terms for the patching below
    ordered = sorted(consts)
    if auto is not None:
        # inline constants back to the rev-specific stock values: the first 15 are the
        # rev-A set, the following 13 are rev B (levels 2..14)
        for k, off in enumerate(ordered):
            lvl = consts[off]
            body[off] = (STOCK_A if k < 15 else STOCK_B)[lvl - 1]
        c1 = bytes.fromhex(auto["cave1"])
        c2 = bytearray(bytes.fromhex(auto["cave2"]))
        for L in range(1, 15):
            struct.pack_into("<h", c2, auto["delta_at"] + (L - 1) * 2, curve[L - 1] - STOCK_A[L - 1])
            struct.pack_into("<h", c2, auto["delta_at"] + 30 + (L - 1) * 2, curve[L - 1] - STOCK_B[L - 1])
        # L15 deltas (index 14) stay 0: level 15 is not changed
        struct.pack_into("<h", c2, auto["delta_at"] + 28, 0)
        struct.pack_into("<h", c2, auto["delta_at"] + 58, 0)
        body[auto["cave1_at"] - HEADER:auto["cave1_at"] - HEADER + len(c1)] = c1
        body[auto["cave2_at"] - HEADER:auto["cave2_at"] - HEADER + len(c2)] = c2
        body[auto["tagcall_at"] - HEADER:auto["tagcall_at"] - HEADER + 4] = bytes.fromhex(auto["tagcall_new"])
        body[auto["pop_at"] - HEADER:auto["pop_at"] - HEADER + 4] = bytes.fromhex(auto["pop_new"])
        used = "auto"
    elif stock:
        used = "stock"
    else:
        for off, lvl in consts.items():
            if lvl <= 14:
                body[off] = curve[lvl - 1]
        used = "spread"

    if load != 1.0:
        lb = sig_load_block(body)
        vals = [LOAD_K0] + [f * load for f in LOAD_FACTORS]
        body[lb:lb + 64] = struct.pack("<8d", *vals)

    crc_compensate(body, COMPENSATE_AT, crc)
    return hdr + bytes(body), dict(setup=ins[start].address, mode=used, consts=consts)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="See the module docstring (python3 -c 'import b21graypatch; print(b21graypatch.__doc__)').")
    ap.add_argument("firmware", help="official B21_Pro_<ver>.bin or a *_noprime10_crcfix.bin")
    ap.add_argument("-o", "--output")
    ap.add_argument("--profile", choices=list(PROFILES) + ["custom"], default="graycum")
    ap.add_argument("--curve", help="14 comma-separated increments c_1..c_14 (with --profile custom)")
    ap.add_argument("--load", type=float, default=1.0,
                    help="net load-term factor (1 = stock, 2 = current best on third-party paper)")
    ap.add_argument("--mode", choices=["best", "auto", "spread"], default="best")
    a = ap.parse_args()

    if a.profile == "custom":
        if not a.curve:
            sys.exit("--profile custom needs --curve")
        curve = [int(x, 0) for x in a.curve.split(",")]
        if len(curve) != 14:
            sys.exit("--curve needs exactly 14 values (levels 1..14; level 15 is never changed)")
    else:
        curve = PROFILES[a.profile]
    if any(not 0 <= c <= 255 for c in curve):
        sys.exit("curve values must be 0..255")
    if not 0 < a.load <= 4:
        sys.exit("--load must be in (0, 4]")

    fw = open(a.firmware, "rb").read()
    out, info = patch(fw, curve, a.load, a.mode)
    if a.output:
        name = a.output
    else:
        stem = a.firmware[:-len("_crcfix.bin")] if a.firmware.endswith("_crcfix.bin") else a.firmware.rsplit(".", 1)[0]
        name = "%s_%s%s_crcfix.bin" % (stem, a.profile,
                                       "_load%d" % round(a.load * 100) if a.load != 1.0 else "")
    open(name, "wb").write(out)

    diff = sum(1 for x, y in zip(fw, out) if x != y)
    print("setup   %08x  gray heat setup function" % info["setup"])
    print("mode    %s%s" % (info["mode"], "  (stock curve for genuine gray rolls)" if info["mode"] == "auto" else ""))
    print("curve   %s  (c_1..c_14)" % " ".join(map(str, curve)))
    print("load    x%g" % a.load)
    print("changed %d bytes, header unchanged, crc32 %08x" % (diff, zlib.crc32(out[HEADER:]) & 0xffffffff))
    print("wrote   %s" % name)


if __name__ == "__main__":
    main()
