"""Codex bans for the mod's own jewels (runs after custom_jewels.py, which owns the mask).

The Codex is a tab of the Armory screen (Bevy UI, screens/codex.rs). Its selection is the
first field of the Armory resource, a 24-byte key {u64 tag, u64 index, ..}; tag 0 = jewel
table index.

- Toggle: double-click a mod jewel's cell. The Codex click system walks the cells whose
  Interaction changed and, on Pressed, copies the cell's key into the selection unless it is
  selected already. Our hook sees every such press before that check: two presses on the
  same mod jewel within GetDoubleClickTime() flip its bit and mark the Armory changed the way
  the game does (*changed = this_run), so the tab is rebuilt at once. A press on any other
  cell resets the pair; clicks outside the cells never reach this system.
- Grid: the cell closure draws "?" over an undiscovered icon; a discovered banned mod jewel
  keeps its icon and gets a red "BANNED" there instead.
- Detail: the panel's one-off "Picked again, .." line (shown once, under the five rarity
  lines) becomes the banned notice, or gets the double-click hint appended.
All texts go through the game's tr(), so lang/<code>.json translates them.
"""
import re
import struct

JEWEL = 0x48
KIND = 0x44  # u8 in a jewel entry, picks the detail panel's "Picked again" line
PICKED = b"Picked again, it grows by nearly the pick's whole worth every time."
PICKED_KINDS = (0, 1, 3, 4, 7)  # kinds whose detail shows PICKED
ALLOWED_LINE = PICKED + b" Double-click in the Codex: disable."
BANNED_LINE = b"BANNED: never offered. Double-click in the Codex: allow."
BANNED_MARK = b"BANNED"
MARK_STYLE = (0.36, 0.95, 0.25, 0.2, 1.0)  # font size relative to the "?", then Srgba

# Stack/frame slots and closure offsets below are read out of the matched game code (wildcards in the
# signatures), so a rebuilt frame moves them with it; each is cross-checked against another game use.
# click system: Pressed check (cmp byte [..], 0 / jne), maybe the leas of the key address, then
# rcx = [[rsp+sel]] = the selection's tag / cmp rcx, -1 / je / cmp rcx, [key] with key = the pressed cell's key.
# Build 109 lays the table loop and the archetype loop out alike (two hits); the table loop comes first.
CLICK = re.compile(rb"\x80(?:[\x38-\x3b\x3e\x3f]|\x3c.)\x00\x0f\x85....(?:\x48\x8d..){0,2}"
                   rb"(\x48\x8b\x4c\x24(.)\x48\x8b\x09)\x48\x83\xf9\xff\x74.(?=[\x48\x49]\x3b)", re.S)
# the game marking the Armory changed: mov rcx, [rsp+changed] / mov edx, [rsp+this_run] / mov [rcx], edx
MARK_CHANGED = re.compile(rb"\x48\x8b\x4c\x24(.)\x8b\x54\x24(.)\x89\x11", re.S)
# cell closure, env in rbx: +0 &key, then &cell size, &discovered, &font at the offsets the code loads
CELL = re.compile(rb"\x48\x8b\x43(?P<discovered>.)\x80\x38\x00\x0f\x85(?P<skip>....)\x48\x8b\x43(?P<size>.)"
                  rb"\x48\x8b\x5b(?P<font>.)\xf3\x0f\x10\x30\xe8(?P<prep>....)\xb9\x01\x00\x00\x00\xba\x01\x00\x00\x00"
                  rb"\xe8(?P<alloc>....)\x48\x85\xc0\x0f\x84(?P<oom>....)\xc6\x00\x3f", re.S)
# mov [rbp+t], r15 / lea rcx, [rbp+x] / lea r8, [rbp+t]: the text bundle is handed to the spawn
CELL_TAIL = re.compile(rb"(\x4c\x89\xbd(.{4}))(\x48\x8d\x4d.)\x4c\x8d\x85\2", re.S)
# the "?" String {cap 1, ptr, len 1}, the font size (xmm6) and the Srgba loaded from a constant
TEXT_STRING = re.compile(rb"\x48\xc7\x85(.{4})\x01\x00\x00\x00\x48\x89\x85(.{4})\x48\xc7\x85(.{4})\x01\x00\x00\x00", re.S)
FONT_STORE = re.compile(rb"\xf3\x0f\x11\xb5(.{4})", re.S)
COLOR_STORE = re.compile(rb"\x0f\x28\x05(.{4})\x0f\x11\x85(.{4})", re.S)
# detail panel: mov rax, [rbp+jewel] / movzx eax, byte [rax+KIND]
DETAIL_JEWEL = re.compile(rb"\x48\x8b\x85(.{4})\x0f\xb6\x40" + bytes([KIND]), re.S)


def imports(p, *names):
    p.pe.parse_data_directories(directories=[1])
    found = {i.name.decode(): i.address for e in p.pe.DIRECTORY_ENTRY_IMPORT for i in e.imports if i.name}
    p.check(all(n in found for n in names), f"imports missing: {[n for n in names if n not in found]}")
    return [found[n] for n in names]


def one(p, pattern, what):
    hits = list(pattern.finditer(p.d))
    p.check(len(hits) == 1, f"{what}: {len(hits)} matches")
    return hits[0]


def rel(p, m, group):
    """Target of the rel32 in a regex group (the instruction ends right after it)."""
    return p.va(m.end(group)) + struct.unpack("<i", m.group(group))[0]


def string(p, s):
    return p.add_data(len(s), s), len(s)


def patch(p):
    p.check("bans_rva" in p.meta, "custom_jewels.py must run first (no bans block)")
    mask = p.ib + p.meta["bans_rva"] + 16
    table = p.ib + p.meta["jewel_table_rva"]
    base, count = p.meta["jewel_base_index"], len(p.meta["bans_ids"])
    p.check(count <= 32, "the ban mask is one u32: at most 32 mod jewels")
    entries = [table + (base + k) * JEWEL for k in range(count)]
    kinds = [p.data[e - p.data_va + KIND] for e in entries]
    p.check(all(k in PICKED_KINDS for k in kinds), f"mod jewel kinds {kinds}: no 'Picked again' line")
    get_module, get_proc = imports(p, "GetModuleHandleA", "GetProcAddress")

    # detail panel: lea rcx, [PICKED] / mov edx, len / call tr
    detail = [m for m in re.finditer(rb"\x48\x8d\x0d(....)\xba" + struct.pack("<I", len(PICKED)) + rb"\xe8(....)", p.d, re.S)
              if bytes(p.d[p.off(rel(p, m, 1)):p.off(rel(p, m, 1)) + len(PICKED)]) == PICKED]
    p.check(len(detail) == 1, f"detail 'Picked again' line: {len(detail)} matches")
    detail = detail[0]
    slots = {m.group(1) for m in DETAIL_JEWEL.finditer(p.d, detail.start() - 0x30, detail.start())}
    p.check(len(slots) == 1, f"detail: {len(slots)} jewel pointer slots")
    detail_jewel = struct.unpack("<i", slots.pop())[0]
    tr = rel(p, detail, 2)
    picked = rel(p, detail, 1)

    allowed, banned = string(p, ALLOWED_LINE), string(p, BANNED_LINE)
    pick_line = "".join(f"""
            lea r8, [{e:#x}]
            cmp rax, r8
            jne other{k}
            bt dword ptr [r9], {k}
            jc banned
            jmp allowed
        other{k}:""" for k, e in enumerate(entries))
    site = p.va(detail.start())
    detail_cave = p.add_cave(lambda va: p.asm(f"""
            mov rax, qword ptr [rbp + {detail_jewel:#x}]
            lea r9, [{mask:#x}]
            {pick_line}
            lea rcx, [{picked:#x}]
            mov edx, {len(PICKED)}
            jmp back
        allowed:
            lea rcx, [{allowed[0]:#x}]
            mov edx, {allowed[1]}
            jmp back
        banned:
            lea rcx, [{banned[0]:#x}]
            mov edx, {banned[1]}
        back:
            jmp {site + 12:#x}
    """, va))
    p.write(site, p.asm(f"jmp {detail_cave:#x}", site).ljust(12, b"\x90"), expect=detail.group(0)[:12])

    # grid cell: "BANNED" in place of the undiscovered "?"; r14 (free there) carries the text length
    cell = one(p, CELL, "codex cell closure")
    cell_va = p.va(cell.start())
    skip, prep, alloc, oom = (rel(p, cell, g) for g in ("skip", "prep", "alloc", "oom"))
    env_discovered, env_size, env_font = (cell.group(g)[0] for g in ("discovered", "size", "font"))
    question, after_question = cell_va + 13, p.va(cell.end())
    window = bytes(p.d[cell.end():cell.end() + 0x180])
    tails = list(CELL_TAIL.finditer(window))
    p.check(len(tails) == 1, f"codex cell text tail: {len(tails)} matches")
    tail_m = tails[0]
    tail_slot = struct.unpack("<i", tail_m.group(2))[0]
    tail_lea = struct.unpack("<b", tail_m.group(3)[3:])[0]
    before = window[:tail_m.start()]
    text = list(TEXT_STRING.finditer(before))
    p.check(len(text) == 1, f"codex cell: {len(text)} '?' String stores")
    text_cap, text_ptr, text_len = (struct.unpack("<i", text[0].group(g))[0] for g in (1, 2, 3))
    p.check(text_ptr == text_cap + 8 and text_len == text_cap + 16, "codex cell: '?' String is not {cap, ptr, len}")
    fonts = list(FONT_STORE.finditer(before))
    p.check(len(fonts) == 1, f"codex cell: {len(fonts)} font size stores")
    font_size = struct.unpack("<i", fonts[0].group(1))[0]
    colors = list(COLOR_STORE.finditer(before, fonts[0].end()))
    p.check(len(colors) >= 1, "codex cell: text color store not found")
    color_src = p.va(cell.end() + colors[0].end(1)) + struct.unpack("<i", colors[0].group(1))[0]
    srgba = struct.unpack_from("<4f", p.d, p.off(color_src))
    p.check(all(0.0 <= c <= 1.0 for c in srgba), f"codex cell: {srgba} is not the text's Srgba")
    color = struct.unpack("<i", colors[0].group(2))[0]
    tail = p.va(cell.end() + tail_m.start())
    tail_len = tail_m.end(3) - tail_m.start()
    mark = string(p, BANNED_MARK)
    style = p.add_data(20, struct.pack("<5f", *MARK_STYLE))
    cell_cave = p.add_cave(lambda va: p.asm(f"""
            mov r14d, 1
            mov rax, qword ptr [rbx + {env_discovered:#x}]
            cmp byte ptr [rax], 0
            je {question:#x}
            mov rax, qword ptr [rbx]
            cmp qword ptr [rax], 0
            jne {skip:#x}
            mov rax, qword ptr [rax + 8]
            sub rax, {base}
            cmp rax, {count}
            jae {skip:#x}
            lea rcx, [{mask:#x}]
            bt dword ptr [rcx], eax
            jnc {skip:#x}
            mov rax, qword ptr [rbx + {env_size:#x}]
            mov rbx, qword ptr [rbx + {env_font:#x}]
            movss xmm6, dword ptr [rax]
            call {prep:#x}
            lea rcx, [{mark[0]:#x}]
            mov edx, {mark[1]}
            call {tr:#x}
            mov qword ptr [rbp + {text_ptr:#x}], rax
            mov r14, rdx
            mov rcx, rdx
            mov edx, 1
            call {alloc:#x}
            test rax, rax
            je {oom:#x}
            push rsi
            push rdi
            mov rdi, rax
            mov rsi, qword ptr [rbp + {text_ptr:#x}]
            mov rcx, r14
            rep movsb
            pop rdi
            pop rsi
            jmp {after_question:#x}
    """, va))
    p.write(cell_va, p.asm(f"jmp {cell_cave:#x}", cell_va).ljust(13, b"\x90"), expect=cell.group(0)[:13])
    tail_cave = p.add_cave(lambda va: p.asm(f"""
            mov qword ptr [rbp + {tail_slot:#x}], r15
            cmp r14, 1
            je plain
            mov qword ptr [rbp + {text_cap:#x}], r14
            mov qword ptr [rbp + {text_len:#x}], r14
            movss xmm0, dword ptr [rbp + {font_size:#x}]
            mulss xmm0, dword ptr [{style:#x}]
            movss dword ptr [rbp + {font_size:#x}], xmm0
            movups xmm0, xmmword ptr [{style + 4:#x}]
            movups xmmword ptr [rbp + {color:#x}], xmm0
        plain:
            lea rcx, [rbp {tail_lea:+#x}]
            jmp {tail + tail_len:#x}
    """, va))
    p.write(tail, p.asm(f"jmp {tail_cave:#x}", tail).ljust(tail_len, b"\x90"), expect=tail_m.group(1) + tail_m.group(3))

    # click system: double-click detection on every press of a cell
    clicks = list(CLICK.finditer(p.d))
    p.check(len(clicks) in (1, 2) and clicks[-1].start() - clicks[0].start() < 0x400,
            f"codex click system: {len(clicks)} matches")
    click = clicks[0]
    click_va = p.va(click.start(1))
    sel = click.group(2)[0]
    cmp_key, jne, key_index = p.insns(click.end(), 3)
    m = re.fullmatch(r"rcx, qword ptr \[(\w+)\]", cmp_key.op_str)
    p.check(cmp_key.mnemonic == "cmp" and m and jne.mnemonic == "jne" and key_index.mnemonic == "mov"
            and key_index.op_str == f"rdx, qword ptr [{m.group(1)} + 8]", "codex click system: key compare changed")
    key = m.group(1)
    marks = {(m.group(1)[0], m.group(2)[0]) for m in MARK_CHANGED.finditer(p.d, click.end(), click.end() + 0x400)}
    p.check(len(marks) == 1, f"codex click system: {len(marks)} changed/this_run slot pairs")
    changed, this_run = marks.pop()
    frame = 7 * 8 + 0x98  # our pushes and locals; rsp stays 16-aligned for the calls
    last = p.add_data(8, struct.pack("<iI", -1, 0))  # +0 mod jewel of the last press (-1 none), +4 its tick
    fns = p.add_data(16)  # GetTickCount, GetDoubleClickTime (resolved on first use)
    names = [string(p, s + b"\0")[0] for s in (b"kernel32.dll", b"GetTickCount", b"user32.dll", b"GetDoubleClickTime")]

    def resolve(dll, name, slot):
        return f"""
            lea rcx, [{dll:#x}]
            lea rax, [{get_module:#x}]
            call qword ptr [rax]
            mov rcx, rax
            lea rdx, [{name:#x}]
            lea rax, [{get_proc:#x}]
            call qword ptr [rax]
            lea r11, [{fns:#x}]
            mov qword ptr [r11 + {slot}], rax"""

    click_cave = p.add_cave(lambda va: p.asm(f"""
            push rax
            push rcx
            push rdx
            push r8
            push r9
            push r10
            push r11
            sub rsp, 0x98
            movdqu xmmword ptr [rsp + 0x20], xmm0
            movdqu xmmword ptr [rsp + 0x30], xmm1
            movdqu xmmword ptr [rsp + 0x40], xmm2
            movdqu xmmword ptr [rsp + 0x50], xmm3
            movdqu xmmword ptr [rsp + 0x60], xmm4
            movdqu xmmword ptr [rsp + 0x70], xmm5
            mov rax, {key}
            mov r10, -1
            cmp qword ptr [rax], 0
            jne remember
            mov rax, qword ptr [rax + 8]
            sub rax, {base}
            cmp rax, {count}
            jae remember
            mov qword ptr [rsp + 0x80], rax
            lea r11, [{fns:#x}]
            cmp qword ptr [r11 + 8], 0
            jne resolved
            {resolve(names[0], names[1], 0)}
            {resolve(names[2], names[3], 8)}
        resolved:
            lea r11, [{fns:#x}]
            cmp qword ptr [r11], 0
            je out
            cmp qword ptr [r11 + 8], 0
            je out
            call qword ptr [r11]
            mov dword ptr [rsp + 0x88], eax
            lea r11, [{fns:#x}]
            call qword ptr [r11 + 8]
            mov r10, qword ptr [rsp + 0x80]
            mov ecx, dword ptr [rsp + 0x88]
            lea r11, [{last:#x}]
            cmp dword ptr [r11], r10d
            jne first
            mov edx, ecx
            sub edx, dword ptr [r11 + 4]
            cmp edx, eax
            ja first
            lea r8, [{mask:#x}]
            lock btc dword ptr [r8], r10d
            mov rax, qword ptr [rsp + {frame + changed:#x}]
            mov edx, dword ptr [rsp + {frame + this_run:#x}]
            mov dword ptr [rax], edx
            mov r10, -1
            jmp remember
        first:
            mov dword ptr [r11 + 4], ecx
        remember:
            lea r11, [{last:#x}]
            mov dword ptr [r11], r10d
        out:
            movdqu xmm0, xmmword ptr [rsp + 0x20]
            movdqu xmm1, xmmword ptr [rsp + 0x30]
            movdqu xmm2, xmmword ptr [rsp + 0x40]
            movdqu xmm3, xmmword ptr [rsp + 0x50]
            movdqu xmm4, xmmword ptr [rsp + 0x60]
            movdqu xmm5, xmmword ptr [rsp + 0x70]
            add rsp, 0x98
            pop r11
            pop r10
            pop r9
            pop r8
            pop rdx
            pop rcx
            pop rax
            mov rcx, qword ptr [rsp + {sel:#x}]
            mov rcx, qword ptr [rcx]
            jmp {click_va + len(click.group(1)):#x}
    """, va))
    p.write(click_va, p.asm(f"jmp {click_cave:#x}", click_va).ljust(len(click.group(1)), b"\x90"), expect=click.group(1))
