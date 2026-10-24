"""Co-op: enemies spawn and stay around the partner's ally too, not only around my hero.

The run server knows only my hero, so stock enemies appear on a ring around it and are recycled
once they are farther than 55 yd from it; the ally (coop_ghost, block AZSGHST3) stood in an empty
field and the partner's replayed hits had nothing to land on. Two places in the game decide this:

- Ring position picker (state+0x4c8 hero x, y, z, +0x980 in 105 / +0x9c8 in 107 smoothed hero
  velocity): (out pos, state, min radius, max radius, f32 weight on the stack). It biases the ring
  ahead of the hero's movement, then tries points on it until one is walkable. Callers: the enemy
  spawner for its "anywhere" spawns (weight 0.5: waves, elites, everything with no fixed point;
  it counts the live enemies against the population cap first), the recycler (1.0) and the
  guardian spawn (0).
- Recycler: each run tick a filter collects up to 4 live enemies whose interpolated position is
  more than 55 yd (2D, squared compare) from the hero; each is moved to a fresh ring position by
  the picker and its move is sent.

Hooks, all inert unless the ally is "live": the block's seqlock gives a consistent snapshot with
active == 1, the dead and paused flags clear, and its seq changed less than coop_ghost.TIMEOUT s
of run clock ago (so a crashed companion stops counting).
- The recycler's filter entry refreshes that snapshot once per tick (its iterator holds a pointer
  to the run clock at +0x10).
- Its two distance checks keep an enemy that is far from my hero but within 55 yd of the ally:
  the nearer hero decides. A guest with live shared enemies (coop_shared) keeps every enemy: the host
  recycles them and its moves follow.
- The spawner's and the recycler's picker calls go through a wrapper that alternates while the
  ally is live: every other call runs the stock picker with the hero position swapped for the
  ally's (velocity zero: no lead), then restores both. The spawner's cap and wave sizes are
  untouched, so a batch is split between the two heroes, not doubled. Guardian spawns stay
  around my hero.
Without a live ally every hooked path falls through to the stock code unchanged.

Block "AZSSPWN1" (spawn_rva in the feed json), exe-owned, for tests: +8 u32 last seq, +12 f32 run
clock it was seen at, +16 snapshot {u32 active, f32 x, y, z}, +32 u32 flags, +36 u8 live,
+37 u8 turn, +76 u32 spawns placed around the ally, +80 u32 recycle checks the ally kept.
"""
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coop_ghost as coop  # noqa: E402  block layout, run tick/clock signatures, .pdata lookup

# picker prologue tail: movsd xmm1, [rdx+vel] ; movaps xmm0, xmm1 ; mulps xmm0, xmm1 ;
# movshdup xmm2, xmm0 ; addss xmm2, xmm0 (squared speed, for the lead)
PICK = re.compile(rb"\xf2\x0f\x10\x8a(....)\x0f\x28\xc1\x0f\x59\xc1\xf3\x0f\x16\xd0\xf3\x0f\x58\xd0", re.S)
# the recycler's distance checks: movsd x, [r13] (hero) ; subps ; mulps ; movshdup ; addss ;
# ucomiss d, xmm7 (55 yd squared) ; ja far. Enemy position in xmm1 / xmm0, both are scratch after.
FAR_A = re.compile(rb"\xf2\x41\x0f\x10\x55\x00\x0f\x5c\xca\x0f\x59\xc9\xf3\x0f\x16\xd1\xf3\x0f\x58\xd1"
                   rb"\x0f\x2e\xd7\x0f\x87(....)", re.S)
FAR_B = re.compile(rb"\xf2\x41\x0f\x10\x4d\x00\x0f\x5c\xc1\x0f\x59\xc0\xf3\x0f\x16\xc8\xf3\x0f\x58\xc8"
                   rb"\x0f\x2e\xcf\x0f\x87(....)", re.S)
# filter entry: push rbp ; push r15 ; push r14 (moved to the cave) ... mov r12, [rdx+0x10] (&clock)
FILTER_HEAD = b"\x55\x41\x57\x41\x56"
FILTER_CLOCK = b"\x4c\x8b\x62\x10"
FILTER_CLOCK_USE = b"\xf3\x41\x0f\x10\x14\x24"  # movss xmm2, [r12]
# a picker call passes its weight first: mov dword [rsp+0x20], imm32
WEIGHT = b"\xc7\x44\x24\x20"
W_SPAWN, W_RECYCLE = 0x3F000000, 0x3F800000  # 0.5, 1.0

# own block
LAST_SEQ, SEEN, SNAP, FLAGS, LIVE, TURN = 8, 12, 16, 32, 36, 37
EPOS, SV_POS, SV_VEL, SV_STATE, TIMEOUT, SPAWNS, KEPT = 40, 48, 64, 88, 72, 76, 80
SIZE = 96


def picker(p):
    """(start va, hero position offset, hero velocity offset) of the ring position picker."""
    m = coop.one(p, PICK, "ring position picker")
    lo, hi = coop.func_range(p, p.va(m.start())) or (None, None)
    p.check(lo is not None and p.va(m.start()) - lo < 0x100, "coop spawns: picker prologue not found")
    vel = coop.s32(m.group(1))
    pairs, singles = set(), set()
    for x in p.insns(p.off(lo), (hi - lo) // 2):
        if x.address >= hi:
            break
        mm = re.fullmatch(r"xmm\d+, (qword|dword) ptr \[r\w+ \+ (0x[0-9a-f]+)\]", x.op_str)
        if mm and x.mnemonic in ("movsd", "movss"):
            (pairs if mm.group(1) == "qword" else singles).add(int(mm.group(2), 16))
    pos = {d for d in pairs if d + 8 in singles and d != vel}
    p.check(len(pos) == 1, f"coop spawns: hero position reads in the picker {pos}")
    return lo, pos.pop(), vel


def picker_call(p, pick, weight, what):
    """The one call to the picker right after `mov dword [rsp+0x20], weight`."""
    sites = []
    for m in re.finditer(re.escape(WEIGHT + struct.pack("<I", weight)), p.d):
        for k in range(m.end(), m.end() + 0x40):
            if p.d[k] == 0xE8 and p.va(k + 5) + struct.unpack_from("<i", p.d, k + 1)[0] == pick:
                sites.append(p.va(k))
    p.check(len(sites) == 1, f"coop spawns: {what} picker calls {len(sites)}")
    return sites[0]


def patch(p):
    p.check("ghost_rva" in p.meta, "coop spawns: needs the coop_ghost block")
    block = p.ib + p.meta["ghost_rva"]
    # the run tick adds dt to the run clock (its entry is already coop_ghost's jmp)
    tick = coop.one(p, coop.CLOCK, "run clock")
    clock, tick_fn = coop.s32(tick.group(1)), coop.func_start(p, p.va(tick.start()))

    pick, pos, vel = picker(p)
    spawn_call = picker_call(p, pick, W_SPAWN, "spawner")
    recycle_call = picker_call(p, pick, W_RECYCLE, "recycler")
    p.check(coop.func_start(p, recycle_call) == tick_fn, "coop spawns: recycler outside the run tick")
    p.check(coop.func_start(p, spawn_call) not in (None, tick_fn, pick), "coop spawns: spawner is not its own function")

    far_a, far_b = coop.one(p, FAR_A, "recycle check A"), coop.one(p, FAR_B, "recycle check B")
    filt = coop.func_start(p, p.va(far_a.start()))
    lo = p.off(filt) if filt else 0
    p.check(filt is not None and coop.func_start(p, p.va(far_b.start())) == filt
            and p.d[lo:lo + 5] == FILTER_HEAD and FILTER_CLOCK in p.d[lo:lo + 0x80]
            and FILTER_CLOCK_USE in p.d[lo:far_a.start()], "coop spawns: recycle filter layout disagrees")
    p.check(any(p.d[k] == 0xE8 and p.va(k + 5) + struct.unpack_from("<i", p.d, k + 1)[0] == filt
                for k in range(p.off(recycle_call) - 0x200, p.off(recycle_call))),
            "coop spawns: the run tick doesn't call the recycle filter before the recycler")

    data = p.add_data(SIZE, b"AZSSPWN1".ljust(TIMEOUT, bytes(1)) + struct.pack("<f", coop.TIMEOUT))
    p.meta["spawn_rva"] = data - p.ib

    def base(reg, va, at):
        """lea reg, [data] as the instruction `at` bytes into the cave at va."""
        return f"lea {reg}, [rip + {data - (va + at + 7):#x}]"

    def build_refresh(va):
        # xmm5 = run clock; keeps every register but xmm5 and the flags
        return p.asm(f"""
            push rcx
            {base("rcx", va, 1)}
            push rax
            sub rsp, 16
            movups xmmword ptr [rsp], xmm4
            mov eax, dword ptr [rcx + {block + coop.B_SEQ - data:#x}]
            test al, 1
            jnz r_done
            movups xmm4, xmmword ptr [rcx + {block + coop.B_ACTIVE - data:#x}]
            push rdx
            mov edx, dword ptr [rcx + {block + coop.B_FLAGS - data:#x}]
            cmp eax, dword ptr [rcx + {block + coop.B_SEQ - data:#x}]
            jne r_torn
            cmp eax, dword ptr [rcx + {LAST_SEQ}]
            je r_same
            mov dword ptr [rcx + {LAST_SEQ}], eax
            movss dword ptr [rcx + {SEEN}], xmm5
        r_same:
            movups xmmword ptr [rcx + {SNAP}], xmm4
            mov dword ptr [rcx + {FLAGS}], edx
        r_torn:
            pop rdx
        r_done:
            xor eax, eax
            cmp dword ptr [rcx + {SNAP}], 1
            jne r_set
            test dword ptr [rcx + {FLAGS}], {coop.FLAG_DEAD | coop.FLAG_PAUSED}
            jnz r_set
            subss xmm5, dword ptr [rcx + {SEEN}]
            xorps xmm4, xmm4
            ucomiss xmm5, xmm4
            jb r_set
            ucomiss xmm5, dword ptr [rcx + {TIMEOUT}]
            jae r_set
            inc eax
        r_set:
            mov byte ptr [rcx + {LIVE}], al
            movups xmm4, xmmword ptr [rsp]
            add rsp, 16
            pop rax
            pop rcx
            ret
        """, va)
    refresh = p.add_cave(build_refresh)

    def build_entry(va):
        return p.asm(f"""
            push rax
            mov rax, qword ptr [rdx + 0x10]
            movss xmm5, dword ptr [rax]
            pop rax
            call {refresh:#x}
            push rbp
            push r15
            push r14
            jmp {filt + len(FILTER_HEAD):#x}
        """, va)
    entry = p.add_cave(build_entry)
    p.write(filt, p.asm(f"jmp {entry:#x}", filt), expect=FILTER_HEAD)

    control = block + coop.B_CONTROL

    def hook_far(m, pos_reg, tmp_reg, tag):
        site, far = p.va(m.start()), p.va(m.end()) + coop.s32(m.group(1))
        near = p.va(m.end())

        def build(va):
            # stock check first; only an enemy far from my hero is measured against the ally too. A guest
            # with live shared enemies recycles nothing: the host moves (and recycles) its mirrors
            return p.asm(f"""
                push rax
                {base("rax", va, 1)}
                test byte ptr [rax + {control - data:#x}], {coop.shared.LIVE}
                jz {tag}_stock
                test byte ptr [rax + {control - data:#x}], {coop.shared.GUEST}
                jnz {tag}_near
            {tag}_stock:
                movlps qword ptr [rax + {EPOS}], {pos_reg}
                movsd {tmp_reg}, qword ptr [r13]
                subps {pos_reg}, {tmp_reg}
                mulps {pos_reg}, {pos_reg}
                movshdup {tmp_reg}, {pos_reg}
                addss {tmp_reg}, {pos_reg}
                ucomiss {tmp_reg}, xmm7
                jbe {tag}_near
                cmp byte ptr [rax + {LIVE}], 0
                je {tag}_far
                movsd {pos_reg}, qword ptr [rax + {EPOS}]
                movsd {tmp_reg}, qword ptr [rax + {SNAP + 4}]
                subps {pos_reg}, {tmp_reg}
                mulps {pos_reg}, {pos_reg}
                movshdup {tmp_reg}, {pos_reg}
                addss {tmp_reg}, {pos_reg}
                ucomiss {tmp_reg}, xmm7
                jbe {tag}_kept
            {tag}_far:
                pop rax
                jmp {far:#x}
            {tag}_kept:
                inc dword ptr [rax + {KEPT}]
            {tag}_near:
                pop rax
                jmp {near:#x}
            """, va)
        cave = p.add_cave(build)
        p.write(site, p.asm(f"jmp {cave:#x}", site).ljust(m.end() - m.start(), b"\x90"), expect=m.group(0))

    hook_far(far_a, "xmm1", "xmm2", "fa")
    hook_far(far_b, "xmm0", "xmm1", "fb")

    def build_wrap(va):
        # (out, state, min r, max r, [rsp+0x28] weight): every other call while the ally is live,
        # the stock picker around the ally
        return p.asm(f"""
            push rbx
            {base("rbx", va, 1)}
            movss xmm5, dword ptr [rdx + {clock:#x}]
            call {refresh:#x}
            cmp byte ptr [rbx + {LIVE}], 0
            je w_stock
            xor byte ptr [rbx + {TURN}], 1
            jz w_stock
            sub rsp, 0x30
            mov eax, dword ptr [rsp + 0x60]
            mov dword ptr [rsp + 0x20], eax
            mov qword ptr [rbx + {SV_STATE}], rdx
            mov rax, qword ptr [rdx + {pos:#x}]
            mov qword ptr [rbx + {SV_POS}], rax
            mov eax, dword ptr [rdx + {pos + 8:#x}]
            mov dword ptr [rbx + {SV_POS + 8}], eax
            mov rax, qword ptr [rdx + {vel:#x}]
            mov qword ptr [rbx + {SV_VEL}], rax
            mov rax, qword ptr [rbx + {SNAP + 4}]
            mov qword ptr [rdx + {pos:#x}], rax
            mov eax, dword ptr [rbx + {SNAP + 12}]
            mov dword ptr [rdx + {pos + 8:#x}], eax
            mov qword ptr [rdx + {vel:#x}], 0
            inc dword ptr [rbx + {SPAWNS}]
            call {pick:#x}
            mov rdx, qword ptr [rbx + {SV_STATE}]
            mov rcx, qword ptr [rbx + {SV_POS}]
            mov qword ptr [rdx + {pos:#x}], rcx
            mov ecx, dword ptr [rbx + {SV_POS + 8}]
            mov dword ptr [rdx + {pos + 8:#x}], ecx
            mov rcx, qword ptr [rbx + {SV_VEL}]
            mov qword ptr [rdx + {vel:#x}], rcx
            add rsp, 0x30
            pop rbx
            ret
        w_stock:
            pop rbx
            jmp {pick:#x}
        """, va)
    wrap = p.add_cave(build_wrap)
    for site in (spawn_call, recycle_call):
        p.write(site, p.asm(f"call {wrap:#x}", site), expect=p.asm(f"call {pick:#x}", site))
    p.log.append(f"coop spawns: picker {pick:x} (pos {pos:#x}, vel {vel:#x}), spawn {spawn_call:x}, "
                 f"recycle {recycle_call:x}, filter {filt:x}")
