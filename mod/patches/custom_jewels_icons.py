"""Item icons for the mod's jewels outside the Codex (the character pane, C).

The game turns a jewel's icon name into an item display id through a static
[{&str icon, u64 display}; 50] table (case-insensitive linear search). Icons that aren't
in it get no texture, so the table is copied to .moddata with the mod jewels' icons
appended (display ids from ItemDisplayInfo.dbc whose inventory icon is that name).
"""
import re
import struct

ROW = 24
# ItemDisplayInfo rows using these inventory icons (checked against the client's DBC);
# a new mod jewel with an icon not in the game's table needs its display id here.
DISPLAYS = {
    "inv_jewelry_ring_32": 23897,
    "inv_jewelry_ring_43": 31905,
}


def table_rows(p, va):
    rows = []
    while True:
        o = p.off(va + len(rows) * ROW)
        ptr, n, display = struct.unpack_from("<QQQ", p.d, o)
        if not (p.ib < ptr < p.ib + 0x10000000 and 0 < n < 64 and 0 < display < 100000):
            return rows
        rows.append((bytes(p.d[p.off(ptr):p.off(ptr) + n]), display))


def patch(p):
    p.check("jewel_table_rva" in p.meta, "custom_jewels.py must run first")
    # the table starts with ("INV_Jewelry_Amulet_03", 2624)
    starts = [m.start() for a in re.finditer(rb"INV_Jewelry_Amulet_03", p.d)
              for m in re.finditer(re.escape(struct.pack("<QQQ", p.va(a.start()), 21, 2624)), p.d)]
    p.check(len(starts) == 1, f"icon table: {len(starts)} candidates")
    table = p.va(starts[0])
    rows = table_rows(p, table)
    size = len(rows) * ROW
    known = {icon.lower() for icon, _ in rows}

    # the search function: 3 rip-relative leas of the table + 2 'cmp rax, size' loop bounds
    leas = []
    for m in re.finditer(rb"[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d](.{4})", p.d, re.S):
        if p.va(m.end()) + struct.unpack("<i", m.group(1))[0] == table:
            p.check(p.offsets.resolve_reference(p.va(m.start())) == table, 'icon table: RIP target disagrees')
            leas.append(p.va(m.start()))
    p.check(len(leas) == 3 and max(leas) - min(leas) < 0x200, f"icon table users: {[hex(x) for x in leas]}")
    p.offsets.symbol('icons.table', table, kind='data', evidence='named row and three RIP-relative users')
    lo, hi = p.off(min(leas)) - 0x100, p.off(max(leas)) + 0x100
    cmp = b"\x48\x3d" + struct.pack("<I", size)
    cmps = [p.va(lo + m.start()) for m in re.finditer(re.escape(cmp), p.d[lo:hi])]
    p.check(len(cmps) == 2, f"icon table bounds: {len(cmps)} matches")

    # mod jewels' icons (as custom_jewels.py built them in .moddata) that the table lacks
    jewels = p.ib + p.meta["jewel_table_rva"] + p.meta["jewel_base_index"] * 0x48
    extra = []
    for k in range(len(p.meta["bans_ids"])):
        ptr, n = struct.unpack_from("<QQ", p.data, jewels + k * 0x48 + 0x20 - p.data_va)
        icon = bytes(p.data[ptr - p.data_va:ptr - p.data_va + n])
        if icon.lower() not in known and icon.lower() not in {i.lower() for i, _ in extra}:
            p.check(icon.decode().lower() in DISPLAYS, f"no item display id for icon {icon.decode()}: add it to DISPLAYS")
            extra.append((icon, DISPLAYS[icon.decode().lower()]))
    if not extra:
        return

    old = bytes(p.d[p.off(table):p.off(table) + size])
    names = [p.add_data(len(icon), icon) for icon, _ in extra]
    new = old + b"".join(struct.pack("<QQQ", va, len(icon), display) for va, (icon, display) in zip(names, extra))
    copy = p.add_data(len(new), new)
    for i in range(len(rows) + len(extra)):
        p.add_reloc(copy + i * ROW)  # the icon name pointers
    for lea in leas:
        o = p.off(lea)
        p.write(lea + 3, struct.pack("<i", copy - (lea + 7)), expect=bytes(p.d[o + 3:o + 7]))
    for c in cmps:
        p.write(c + 2, struct.pack("<I", len(new)), expect=struct.pack("<I", size))
    p.log.append(f"icon table: +{len(extra)} ({b', '.join(i for i, _ in extra).decode()}), moved to {copy:#x}")
