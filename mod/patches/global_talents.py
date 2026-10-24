"""Account-wide talent experience.

The only place talent XP changes in game is the award function (add to the class
record, or push a new record). Both paths meet at `shl rsi, 5; xor ecx, ecx`.
There we jump to the cave, which raises every record's XP to the highest one, so
all classes that have a record share the best class's XP. Classes without a
record get one from the launcher's save sync.
"""
import re

# mov ecx, -1 ; cmovae ecx, r8d ; mov [r9+r11-0x20], ecx ; jmp short <join>
AWARD = re.compile(rb"\x47\x03\x44\x19\xe0\xb9\xff\xff\xff\xff\x41\x0f\x43\xc8\x43\x89\x4c\x19\xe0\xeb(.)", re.S)
JOIN = bytes.fromhex("48c1e605" "31c9")  # shl rsi, 5 ; xor ecx, ecx



def before(p, off, pattern):
    """Instruction ending right at off whose `mnemonic op_str` matches pattern (disp8 or disp32 form)."""
    for size in (4, 7):
        i = p.insns(off - size, 1)
        if i and i[0].size == size and (m := re.fullmatch(pattern, f"{i[0].mnemonic} {i[0].op_str}")):
            return off - size, m
    return None, None


def push_path_reloads(p, off):
    off, load = before(p, off, r"mov r9, qword ptr \[rcx \+ (0x[0-9a-f]+)\]")
    if not load:
        return False
    _, store = before(p, off, rf"mov qword ptr \[rcx \+ {int(load[1], 16) + 8:#x}\], rsi")
    return store is not None


def patch(p):
    hits = list(AWARD.finditer(p.d))
    p.check(len(hits) == 1, f"talent award: {len(hits)} signature hits instead of 1")
    m = hits[0]
    join = p.va(m.end()) + m.group(1)[0]
    p.check(p.d[p.off(join):p.off(join) + 6] == JOIN, f"talent award: unexpected join at {join:x}")
    # the push path (class had no record) must fall through into the join too
    # (it ends storing the new len to [rcx+L] and reloading the records ptr from [rcx+L-8])
    p.check(push_path_reloads(p, p.off(join)), "talent award: push path does not reach the join")
    # live at the join: r9 = records ptr, rsi = record count, dl = class, eax = points before.
    # ecx, r8, r10, r11 are rewritten right after it, so the cave may use them.
    resume = join + len(JOIN)
    cave = p.add_cave(lambda va: p.asm(f"""
        mov r11, rsi
        shl r11, 5
        xor ecx, ecx
        xor r10d, r10d
    find_max:
        cmp r10, r11
        jae spread
        mov r8d, dword ptr [r9 + r10]
        cmp r8d, ecx
        cmovae ecx, r8d
        add r10, 0x20
        jmp find_max
    spread:
        xor r10d, r10d
    store:
        cmp r10, r11
        jae done
        mov dword ptr [r9 + r10], ecx
        add r10, 0x20
        jmp store
    done:
        shl rsi, 5
        xor ecx, ecx
        jmp {resume:#x}
    """, va))
    p.write(join, p.asm(f"jmp {cave:#x}", join) + b"\x90", expect=JOIN)
