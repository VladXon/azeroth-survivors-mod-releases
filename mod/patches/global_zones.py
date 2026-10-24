"""Account-wide zone unlocks.

Opened zones live in a Vec of 16-byte entries {u8 class, u64 opened} where opened is
how many zones of the 12 are open (zone index + 1). The only in-game writer is the
end-of-run reward code: beating zone N sets the class entry to N + 2, either by
updating its entry or by pushing a new one. Both paths are redirected to a loop that
raises every entry to that value instead (never lowers one). Classes without an entry
get one from the launcher's save sync at the next start.
"""
import re

# beating a zone, class already has an entry: add rax, 2 / mov [rcx+r8-8], rax / jmp resume
UPDATE = re.compile(rb"\x44\x38\x0c\x11\x4c\x89\xc2\x75(.)"
                    rb"(\x48\x83\xc0\x02\x4a\x89\x44\x01\xf8)\xeb(.)", re.S)


def patch(p):
    up = list(UPDATE.finditer(p.d))
    p.check(len(up) == 1, f"zone unlock writer: {len(up)} matches")
    up = up[0]
    update_va = p.va(up.start(2))
    resume = p.va(up.end()) + int.from_bytes(up.group(3), "little", signed=True)
    # the class search loops `cmp idx, rdx` with rdx stepping 16: idx = len * 16, as the cave expects
    head = p.insns(up.start(2) + int.from_bytes(up.group(1), "little", signed=True), 1)[0]
    m = re.fullmatch(r"(\w+), rdx", head.op_str)
    p.check(head.mnemonic == "cmp" and m, "zone unlock writer: search loop changed")
    idx = m.group(1)
    # no entry yet: cmp len, [vec + cap] / jne push / (grow) / push: add rax, 2 / mov [rcx + idx], class /
    # mov [rcx + idx + 8], rax / inc len / mov [vec + cap + 16], len / resume
    cmp, jne = p.insns(up.end(), 2)
    m = re.fullmatch(r"(\w+), qword ptr \[(\w+) \+ (0x[0-9a-f]+)\]", cmp.op_str)
    p.check(cmp.mnemonic == "cmp" and m and jne.mnemonic == "jne", "zone unlock writer: push check changed")
    length, vec, cap = m.group(1), m.group(2), int(m.group(3), 16)
    tail = [i for i in p.insns(p.off(int(jne.op_str, 16)), 8) if i.address < resume]
    text = "; ".join(f"{i.mnemonic} {i.op_str}" for i in tail)
    p.check(re.fullmatch(rf"add rax, 2; (?:mov rdx, qword ptr \[rbp \+ 0x[0-9a-f]+\]; )?"
                         rf"mov byte ptr \[rcx \+ {idx}\], \w+; mov qword ptr \[rcx \+ {idx} \+ 8\], rax; "
                         rf"inc {length}; mov qword ptr \[{vec} \+ {cap + 16:#x}\], {length}", text)
            and tail[-1].address + tail[-1].size == resume and tail[-1].size >= 5,
            f"zone unlock writer: push path changed: {text}")
    store = tail[-1]

    # rcx = entries, idx = len * 16 (> 0 here), rax = old index; idx is dead after resume
    raise_all = p.add_cave(lambda va: p.asm(f"""
        add rax, 2
    next:
        sub {idx}, 0x10
        cmp qword ptr [rcx + {idx} + 8], rax
        jae keep
        mov qword ptr [rcx + {idx} + 8], rax
    keep:
        test {idx}, {idx}
        jnz next
        jmp {resume:#x}
    """, va))
    # after the push rax already holds the new value and the new entry sits at [rcx + idx]
    after_push = p.add_cave(lambda va: bytes(store.bytes) + p.asm(f"""
        add {idx}, 0x10
        jmp {raise_all + 4:#x}
    """, va + store.size))

    p.write(update_va, p.asm(f"jmp {raise_all:#x}", update_va).ljust(9, b"\x90"), expect=up.group(2))
    p.write(store.address, p.asm(f"jmp {after_push:#x}", store.address).ljust(store.size, b"\x90"),
            expect=bytes(store.bytes))
