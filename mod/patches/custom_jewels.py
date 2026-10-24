"""Mod jewels: extra rings in the in-run jewel pool, declared as data in JEWELS.

The static [Jewel; 22] table is copied to .moddata with the mod jewels appended
(string pointers get base relocations); the slice record every client-side user
reads and the server's offer walk are repointed. Every mod jewel uses kind 0 (plain
"+%", no caps), so offers, rarity rolls, value scaling (base x 1.0/1.4/1.8/2.4/3.2,
x1.5 while not held, / (1 + 0.08 x times held) after) and tooltips work unchanged;
the stat switch skips them and the effect comes from our own hooks:
- "difficulty": every pick adds the picked value to the run's enemy density, 0.4 x it
  to enemy health and 0.2 x it to enemy damage (u16 percentages in the run state, read
  live at spawn and part of the run-end reward multiplier). No cap; each part is rounded
  to nearest, at least +1, and saturates at 0xFFFF;
- "thorns": the picked value goes into the run's gear-bonus aggregate as a thorns
  (kind 22) bonus through the game's own add_bonus, and is re-added whenever the
  game rebuilds that aggregate (it does on amulet picks).

Bans: a .moddata block (see BANS_*) holds a bitmask over JEWELS; the offer walk
skips banned mod jewels, base jewels are never touched. p.meta exposes it.
"""
import re
import struct
from dataclasses import dataclass

JEWEL = 0x48
RING, TRINKET = 0, 1


@dataclass
class Jewel:
    id: str
    name: str  # English; translations go to lang/<code>.json as "jewel.<id>"
    text: str  # English effect line, {v} = value in percent; translated by its own text
    icon: str
    value: float  # common rarity value; the game scales it by rarity
    slot: int
    effect: str  # "difficulty" or "thorns"


JEWELS = [
    Jewel("difficulty", "Ring of Difficulty",
          "+{v}% more enemies; enemy health grows by 40% of that and enemy damage by 20%",
          "INV_Jewelry_Ring_32", 0.125, RING, "difficulty"),  # first pick 19/26/34/45/60% (the game's x1.5)
    Jewel("thorns", "Ring of Thorns", "Melee attackers take +{v}% of the damage they deal you",
          "INV_Jewelry_Ring_43", 0.23, RING, "thorns"),  # 23/32/41/55/74%, closest to 20/30/40/55/75
]

BANS_MAGIC = b"AZSBANS1"
BANS_VERSION = 1
KIND = 0x44  # u8 in a jewel entry (build_table writes it there)
# Run-state offsets are read out of the game's code in patch() (v105 -> v107 moved the gear-bonus
# aggregate 0x770 -> 0x7B8; the difficulty u16s 0x2B0/0x2B2/0x2B4 and held jewels 0x330 stayed).
# Percent per 1.0 of picked value for the u16 density, health, damage percentages
# (the preset line density,health,damage,speed).
DIFFICULTY_SCALES = (100.0, 40.0, 20.0)
THORNS_KIND = 22


def find_table(p):
    """(va, count) of the jewel table: entries start with the str slice of 'accuria'."""
    first = p.d.find(b"accuriaBand of Accuria")
    p.check(first > 0, "jewel strings not found")
    hits = [m.start() for m in re.finditer(re.escape(struct.pack("<QQ", p.va(first), 7)), p.d)]
    p.check(len(hits) == 1, f"jewel table: {len(hits)} candidates")
    count = 0
    while True:
        id_ptr, id_len = struct.unpack_from("<QQ", p.d, hits[0] + count * JEWEL)
        if not (p.ib < id_ptr < p.ib + 0x10000000 and 0 < id_len < 40):
            break
        count += 1
    p.check(count >= 20, f"jewel table: only {count} entries")
    return p.va(hits[0]), count


def one(p, pattern, what):
    hits = list(re.finditer(pattern, p.d, re.S))
    p.check(len(hits) == 1, f"{what}: {len(hits)} matches")
    return hits[0]


def build_table(p, table_va, count):
    """Copies the table with JEWELS appended; returns (new table va, [entry va per mod jewel])."""
    strings = []
    for j in JEWELS:
        fields = [j.id.encode(), j.name.encode(), j.icon.encode(), j.text.encode()]
        strings.append([(p.add_data(len(f), f), len(f)) for f in fields])
    entries = b"".join(struct.pack("<QQQQQQQQfBBH", *(x for s in ss for x in s), j.value, 0, j.slot, 0)
                       for j, ss in zip(JEWELS, strings))
    old = bytes(p.d[p.off(table_va):p.off(table_va) + count * JEWEL])
    table = p.add_data(len(old) + len(entries), old + entries)
    for i in range(count + len(JEWELS)):
        for field in range(4):
            p.add_reloc(table + i * JEWEL + field * 16)
    return table, [table + (count + k) * JEWEL for k in range(len(JEWELS))]


def bonus_call(value_reg, agg, tag):
    """asm: add_bonus(&agg, &Bonus::Stat{kind 22, value}, &None) with the game's own function;
    tag = the Bonus::Stat tag read out of add_bonus. Needs a 16-aligned rsp; clobbers volatile registers only."""
    return f"""
        sub rsp, 0x80
        xorps xmm0, xmm0
        movups xmmword ptr [rsp + 0x20], xmm0
        movups xmmword ptr [rsp + 0x30], xmm0
        movups xmmword ptr [rsp + 0x40], xmm0
        movups xmmword ptr [rsp + 0x50], xmm0
        movups xmmword ptr [rsp + 0x60], xmm0
        mov qword ptr [rsp + 0x70], 0
        mov byte ptr [rsp + 0x20], {tag:#x}
        movss dword ptr [rsp + 0x24], {value_reg}
        mov byte ptr [rsp + 0x28], {THORNS_KIND}
        lea rcx, [{agg}]
        lea rdx, [rsp + 0x20]
        lea r8, [rsp + 0x60]
        call {{add_bonus:#x}}
        add rsp, 0x80
    """


def patch(p):
    table_va, count = find_table(p)
    end = count * JEWEL

    record = one(p, re.escape(struct.pack("<QQ", table_va, count)), "jewel slice record")
    walk = re.compile(rb"\x49\x81\xfe" + re.escape(struct.pack("<I", end)) + rb"(.{2,6}?)\x48\x8d\x05(.{4})", re.S)
    walks = [m for m in walk.finditer(p.d) if p.va(m.end()) + struct.unpack("<i", m.group(2))[0] == table_va]
    p.check(len(walks) == 1, f"jewel table walk: {len(walks)} matches")
    walk = walks[0]
    loop_head = p.va(walk.start())

    # jewel pick: movdqa/movaps xmm6, xmm0 (picked value) / mov [rbp+X], r13 (jewel) / cmp byte [r13+kind], 15
    # (soulstone, the kind the stat switch skips; 16 after 109) / jne / mov r8, [rbp+run]
    pick = one(p, rb"(?:\x66\x0f\x6f\xf0|\x0f\x28\xf0)(\x4c\x89\xad.{4})\x41\x80\x7d(.)(.)\x75.\x4c\x8b\x85(.{4})",
               "jewel pick")
    p.check(pick.group(2)[0] == KIND, f"jewel kind byte moved to {pick.group(2)[0]:#x}")
    skip_kind = pick.group(3)[0]
    run = pick.group(4)
    run_slot = struct.unpack("<i", run)[0]  # [rbp+X] holds the run state
    # next: mov rax, [rbp+run] / mov r15, [rax+held] / mov rsi, [rax+held+8] / test rsi, rsi / je / rsi * 0x18,
    # later addss xmm6, [r15+r13+value]: held jewels are a Vec of {&Jewel, u64, f32 value, u32 picks}
    after = bytes(p.d[pick.end():pick.end() + 0x100])
    held = re.search(rb"\x48\x8b\x85" + re.escape(run) + rb"\x4c\x8b\xb8(.{4})\x48\x8b\xb0(.{4})\x48\x85\xf6\x74."
                     rb"\x48\x8d\x04\xf5\x00\x00\x00\x00\x48\x8d\x3c\x40", after, re.S)
    p.check(held is not None, "held jewels: list load not found after the pick")
    held_ptr = struct.unpack("<i", held.group(1))[0]
    p.check(struct.unpack("<i", held.group(2))[0] == held_ptr + 8, "held jewels: not a (ptr, len) pair")
    value = re.search(rb"\xf3\x43\x0f\x58\x74\x2f(.)\xf3\x43\x0f\x11\x74\x2f\1", after, re.S)
    p.check(value is not None, "held jewels: value field not found")
    held_value = value.group(1)[0]

    # stat switch: movzx esi (edi after 109), byte [r11+kind], then its jump table
    stat_sites = list(re.finditer(rb"\x41\x0f\xb6([\x73\x7b])" + bytes([KIND]) + rb"(?=\x4c\x8d\x15|\x49\x63(?:\x34\xb2|\x3c\xba))", p.d))
    p.check(len(stat_sites) == 2, f"jewel stat switch: {len(stat_sites)} sites")
    lea = p.insns(stat_sites[0].start() + 5, 1)[0]
    p.check(lea.mnemonic == "lea" and lea.op_str.startswith("r10, [rip"), "jewel stat switch: no jump table")
    jt = lea.address + lea.size + int(lea.op_str.split("+ ")[1].rstrip("]"), 16)
    skip = jt + struct.unpack_from("<i", p.d, p.off(jt) + 4 * skip_kind)[0]
    p.check(p.d[p.off(skip):p.off(skip) + 4] == b"\x49\x83\xc1\x18", f"jewel stat switch: kind {skip_kind} is not 'next'")

    # the difficulty u16s, each read out of the code that scales enemies by it:
    # damage: movzx eax, word [reg+damage] / xorps / cvtsi2ss / divss [100.0] / mulss xmm0, xmm1
    damage = one(p, rb"\x0f\xb7[\x80-\x87](.{4})\x0f\x57\xc0\xf3\x0f\x2a\xc0\xf3\x0f\x5e\x05.{4}\xf3\x0f\x59\xc1",
                 "enemy damage")
    # health: movzx edi, word [rsi+health] / mov esi, [rsi+X] / mov rdx, [rbp+Y] / test rdx, rdx
    health = one(p, rb"\x0f\xb7\xbe(.{4})\x8b\xb6.{4}\x48\x8b\x55.\x48\x85\xd2", "enemy health")
    # spawn count: movzx eax, word [reg+density] / cvtsi2ss / divss [100.0] / mulss [c] / cvttss2si rax
    density = one(p, rb"\x0f\xb7[\x80-\x87](.{4})\xf3\x0f\x2a\xc0\xf3\x0f\x5e\x05.{4}\xf3\x0f\x59\x05.{4}"
                  rb"\xf3\x48\x0f\x2c\xc0", "enemy density")
    fields = [struct.unpack("<i", m.group(1))[0] for m in (density, health, damage)]
    p.check(fields[1] == fields[0] + 2 and fields[2] == fields[0] + 4,
            f"difficulty fields {[hex(x) for x in fields]}: not consecutive density, health, damage u16s")

    # end of the gear-bonus rebuild: movups [rsi+agg+0x10], xmm1 / movups [rsi+agg], xmm0 / mov rbx, [rsi+X]
    rebuild = one(p, rb"\x0f\x11\x8e(.{4})(\x0f\x11\x86(.{4}))\x48\x8b\x5e.", "bonus rebuild")
    bonus_agg = struct.unpack("<i", rebuild.group(3))[0]
    p.check(struct.unpack("<i", rebuild.group(1))[0] == bonus_agg + 0x10, "bonus rebuild: not the aggregate's stores")
    # the game's own add on a jewel pick: mov rax, [rbp+run] / lea rdx, [rax+agg]
    p.check(b"\x48\x8b\x85" + run + b"\x48\x8d\x90" + rebuild.group(3) in p.d[pick.start() - 0x1000:pick.start()],
            "bonus aggregate offset mismatch")
    # add_bonus(&agg, &Bonus, &Option): the one function the rebuild calls whose body starts with
    # movzx eax, byte [rdx] / sub al, <the Bonus::Stat tag bonus_call builds: 0x23, 0x24 after 109>
    text = p.sec(b".text")
    text_lo, text_hi = p.ib + text.VirtualAddress, p.ib + text.VirtualAddress + text.Misc_VirtualSize
    calls = {}
    window = rebuild.start() - 0x300
    for m in re.finditer(rb"\xe8(?=(.{4}))", p.d[window:rebuild.start()], re.S):  # overlapping: stray e8 bytes
        target = p.va(window + m.end(1)) + struct.unpack("<i", m.group(1))[0]
        if text_lo <= target < text_hi:
            tag = re.search(rb"\x0f\xb6\x02\x2c(.)", p.d[p.off(target):p.off(target) + 0x40], re.S)
            if tag:
                calls[target] = tag.group(1)[0]
    p.check(len(calls) == 1, f"add_bonus: {len(calls)} candidates called by the rebuild")
    add_bonus, stat_tag = calls.popitem()

    table, entries = build_table(p, table_va, count)
    by_effect = {j.effect: e for j, e in zip(JEWELS, entries)}

    p.check(len(JEWELS) <= 32, "the ban mask is one u32: at most 32 mod jewels")
    bans = p.add_data(48, BANS_MAGIC + struct.pack("<III", BANS_VERSION, len(JEWELS), 0))
    p.meta.update(bans_rva=bans - p.ib, bans_ids=[j.id for j in JEWELS],
                  jewel_table_rva=table - p.ib, jewel_base_index=count)

    p.write(p.va(record.start()), struct.pack("<QQ", table, count + len(JEWELS)),
            expect=struct.pack("<QQ", table_va, count))
    p.write(loop_head, b"\x49\x81\xfe" + struct.pack("<I", end + len(JEWELS) * JEWEL), expect=walk.group(0)[:7])

    # offer walk: r14 = offset of the entry about to be considered; banned mod jewels are skipped
    lea_va = p.va(walk.end() - 7)
    checks = "".join(f"""
            cmp r14, {end + k * JEWEL:#x}
            jne next{k}
            bt dword ptr [{bans + 16:#x}], {k}
            jc banned
        next{k}:""" for k in range(len(JEWELS)))
    cave = p.add_cave(lambda c: p.asm(f"""
            {checks}
            lea rax, [{table:#x}]
            jmp {lea_va + 7:#x}
        banned:
            add r14, {JEWEL:#x}
            jmp {loop_head:#x}
        """, c))
    p.write(lea_va, p.asm(f"jmp {cave:#x}", lea_va).ljust(7, b"\x90"), expect=walk.group(0)[-7:])

    for site in stat_sites:
        va = p.va(site.start())
        r = {0x73: "si", 0x7b: "di"}[site.group(1)[0]]  # the switch's own destination: the only register free here
        skips = "".join(f"""
            lea r{r}, [{e:#x}]
            cmp r11, r{r}
            je skip""" for e in entries)
        cave = p.add_cave(lambda c, va=va, r=r: p.asm(f"""
            {skips}
            movzx e{r}, byte ptr [r11 + {KIND:#x}]
            jmp {va + 5:#x}
        skip:
            mov e{r}, {skip_kind}
            jmp {va + 5:#x}
        """, c))
        p.write(va, p.asm(f"jmp {cave:#x}", va), expect=site.group(0))

    # pick: rax, rcx, rdx, r8-r11, xmm0-5 and flags are dead here, rsp is 16-aligned
    scales = list(DIFFICULTY_SCALES)
    consts = p.add_data(4 * len(scales) + 8, struct.pack(f"<{len(scales) + 2}f", *scales, 1.0, 65535.0))
    lo, hi = consts + 4 * len(scales), consts + 4 * len(scales) + 4
    # clamped to [1, 0xFFFF] as a float first (maxss also turns NaN into 1); cvtss2si rounds to nearest
    difficulty = "".join(f"""
            movss xmm0, xmm6
            mulss xmm0, dword ptr [{consts + 4 * i:#x}]
            maxss xmm0, dword ptr [{lo:#x}]
            minss xmm0, dword ptr [{hi:#x}]
            cvtss2si eax, xmm0
            movzx ecx, word ptr [rdx + {field:#x}]
            add ecx, eax
            cmp ecx, 0xffff
            jbe fits{i}
            mov ecx, 0xffff
        fits{i}:
            mov word ptr [rdx + {field:#x}], cx""" for i, field in enumerate(fields))
    site = p.va(pick.start(1))
    displaced = pick.group(1)
    thorns = bonus_call("xmm6", f"rcx + {bonus_agg:#x}", stat_tag)
    cave = p.add_cave(lambda c: displaced + p.asm(f"""
            lea rax, [{by_effect["difficulty"]:#x}]
            cmp r13, rax
            jne not_difficulty
            mov rdx, qword ptr [rbp + {run_slot:#x}]
            {difficulty}
            jmp done
        not_difficulty:
            lea rax, [{by_effect["thorns"]:#x}]
            cmp r13, rax
            jne done
            mov rcx, qword ptr [rbp + {run_slot:#x}]
            {thorns.format(add_bonus=add_bonus)}
        done:
            jmp {site + 7:#x}
        """, c + len(displaced)))
    p.write(site, p.asm(f"jmp {cave:#x}", site).ljust(7, b"\x90"), expect=displaced)

    # after the game rebuilds the gear-bonus aggregate (rsi = run state), re-add held thorns rings
    site = p.va(rebuild.start(2))
    displaced = rebuild.group(2)
    cave = p.add_cave(lambda c: displaced + p.asm(f"""
            mov rcx, qword ptr [rsi + {held_ptr:#x}]
            mov rdx, qword ptr [rsi + {held_ptr + 8:#x}]
            lea rax, [{by_effect["thorns"]:#x}]
        scan:
            test rdx, rdx
            jz done
            cmp qword ptr [rcx], rax
            je found
            add rcx, 0x18
            dec rdx
            jmp scan
        found:
            movss xmm1, dword ptr [rcx + {held_value:#x}]
            {bonus_call("xmm1", f"rsi + {bonus_agg:#x}", stat_tag).format(add_bonus=add_bonus)}
        done:
            jmp {site + 7:#x}
        """, c + len(displaced)))
    p.write(site, p.asm(f"jmp {cave:#x}", site).ljust(7, b"\x90"), expect=displaced)
