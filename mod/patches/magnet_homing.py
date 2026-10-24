"""Visible wisp pull: slower stock flights, and homing for the magnet and long flights.

Stock magnet (one function, called when the hero touches a magnet) sets every idle
experience/gold wisp to "flying" with arrival = now + max(dist / 40, 0.15) and sends one
fly packet aimed at where the hero stood, so the whole map lands within ~2 s.

Here the magnet flies slower (PULL_SPEED, MIN_FLIGHT) and marks its wisps in the entry's
padding (+0x44: "MG" << 16 | next re-aim time in 1/16 s). In the run tick's pickup loop,
a marked flying wisp is re-aimed every INTERVAL: its position moves along its flight,
arrival is recomputed toward the hero's current position, and the entry is handed to the
stock "start flying" list, so the original code sends the fly packet (no calls from the
caves). Collection stays time-based (now >= arrival).

Stock pickups (a wisp entering the pickup radius) fly at STOCK_SPEED / STOCK_MIN instead
of 24 yd/s / 0.12 s, and stale wisps (older than 6 s, pulled from anywhere) at PULL_SPEED
instead of 32 yd/s, so the pull is visible. A flight longer than LONG gets the same mark
and homes at PULL_SPEED; shorter ones get the mark cleared (the padding may hold garbage).

Pickup entry (0x48 bytes in v105 and v107): +0x18 u32 state (0 idle, 1 flying), +0x1c f32 arrival,
+0x20 u32 class (0 = wisp), +0x28 guid, +0x34 f32 x, y, z, +0x40 spawn time,
+0x44 padding (never written by the game). Every offset is read out of the matched game code
and cross-checked between the magnet and the run tick, so a reshuffled entry fails the build.
"""
import re
import struct

PULL_SPEED = 14.0  # yd/s: magnet, stale wisps and every re-aim; faster than the hero so wisps catch up
MIN_FLIGHT = 0.6
MIN_REAIM = 0.15
INTERVAL = 0.25  # s between re-aims of one wisp
CAP = 32  # re-aims per tick, the rest wait for the next tick
MARK = 0x4D47
STOCK_SPEED = 10.0  # stock in-radius flight, was 24
STOCK_MIN = 0.45  # was 0.12
LONG = 0.5  # stock flights longer than this home in
MAX_LEFT = 30.0  # a marked wisp further than this from arrival is not ours: leave it alone

# data block: f32 PULL_SPEED, MIN_FLIGHT, MIN_REAIM, INTERVAL, 16, 1/16, 1, 0, last_now; u32 count, cap;
# f32 LONG, MAX_LEFT, STOCK_SPEED, STOCK_MIN
DATA = struct.pack("<9f2I4f", PULL_SPEED, MIN_FLIGHT, MIN_REAIM, INTERVAL, 16.0, 1 / 16, 1.0, 0.0, -1.0, 0, CAP,
                   LONG, MAX_LEFT, STOCK_SPEED, STOCK_MIN)

# Stack slots, rip displacements and entry field offsets are wildcards: they shift between builds.
# magnet pull: movss xmm9, [speed 40]; movss xmm10, [min 0.15]; lea rdi, [rbp+x]
MAGNET_CONSTS = re.compile(rb"\xf3\x44\x0f\x10\x0d(.{4})\xf3\x44\x0f\x10\x15(.{4})\x48\x8d\x7d.", re.S)
# ...its loop over entries: add rbx, SIZE; lea rcx, [rbx-SIZE]; cmp rcx, rax; je; cmp dword [rcx+class], 0
MAGNET_LOOP = re.compile(rb"\x48\x83\xc3(.)\x48\x8d\x4b(.)\x48\x39\xc1\x0f\x84.{4}\x83\x79(.)\x00", re.S)
# ...dist / xmm9, max xmm10: divss xmm11, xmm9; maxss xmm11, xmm10
MAGNET_FLIGHT = bytes.fromhex("f3450f5ed9" "f3450f5fda")
# ...rbx = entry end: mov dword [rbx+state], 1; movss [rbx+arrival], xmm0; mov r15, [rbx+guid]
MAGNET_MARK = re.compile(rb"\xc7\x43(.)\x01\x00\x00\x00\xf3\x0f\x11\x43(.)\x4c\x8b\x7b(.)", re.S)
# run tick: movss xmm10, [6.0]; movss xmm11, [32 stale]; movss xmm12 (xmm13 after 109), [24 stock];
# movss xmm14, [0.12 min]; lea r13, [rbp+x]
TICK_CONSTS = re.compile(rb"\xf3\x44\x0f\x10\x15.{4}\xf3\x44\x0f\x10\x1d.{4}\xf3\x44\x0f\x10([\x25\x2d]).{4}"
                         rb"\xf3\x44\x0f\x10\x35.{4}\x4c\x8d\xad.{4}", re.S)
# run tick pickup loop: lea r12, [rax+SIZE]; cmp dword [rax+class], 0; jne; cmp byte [rax+state], 0; jne skip;
# movsd xmm0, [rax+pos]; subps xmm0, xmm7
TICK = re.compile(rb"\x4c\x8d\x60(.)\x83\x78(.)\x00\x0f\x85.{4}(\x80\x78(.)\x00\x0f\x85(.{4}))"
                  rb"\xf2\x0f\x10\x40(.)\x0f\x5c\xc7", re.S)
# ...before it, the stale check: mov rax, r12 (r12 = entry); movaps xmm0, xmm8; subss xmm0, [r12+spawn];
# ucomiss xmm0, xmm10
TICK_STALE = re.compile(rb"\x4c\x89\xe0\x41\x0f\x28\xc0\xf3\x41\x0f\x5c\x44\x24(.)\x41\x0f\x2e\xc2", re.S)
# ...its "start flying" tail: mov dword [rax+state], 1; movss [rax+arrival], xmm0; mov reg64, [rax+guid]
# (rbx up to v107, r14 in v109)
TICK_PUSH = re.compile(rb"\xc7\x40(.)\x01\x00\x00\x00\xf3\x0f\x11\x40(.)([\x48\x4c]\x8b[\x40\x48\x50\x58\x60\x68\x70\x78](.))",
                       re.S)
LOOP_FLAG = re.compile(rb"\x44\x88\x95.{4}", re.S)  # mov byte [rbp+X], r10b, replayed before the push


def one(p, rx, what, start=0, end=None):
    hits = list(rx.finditer(p.d, start, end if end is not None else len(p.d)))
    p.check(len(hits) == 1, f"magnet homing: {what}: {len(hits)} hits instead of 1")
    return hits[0]


def s8(m, group):
    return int.from_bytes(m.group(group), "little", signed=True)


def entry_layout(p, magnet, mark, tick, stale, push):
    """Pickup entry offsets read from the magnet and the run tick; both must agree."""
    size = s8(magnet, 1)
    e = dict(size=size, cls=s8(magnet, 3), state=size + s8(mark, 1), arrival=size + s8(mark, 2),
             guid=size + s8(mark, 3), pos=s8(tick, 6), spawn=s8(stale, 1), pad=size - 4)
    seen = dict(size=(-s8(magnet, 2), s8(tick, 1)), cls=(s8(tick, 2),), state=(s8(tick, 4), s8(push, 1)),
                arrival=(s8(push, 2),), guid=(s8(push, 4),))
    for k, vals in seen.items():
        p.check(all(v == e[k] for v in vals), f"magnet homing: entry {k} offsets disagree: {e[k]:#x} vs {vals}")
    # the mark lives in the trailing padding: it must sit after every field the game uses
    used = [e[k] for k in ("state", "arrival", "cls", "guid", "spawn")] + [e["pos"] + 8]
    p.check(e["pad"] >= max(used) + 4 and e["pad"] % 4 == 0, f"magnet homing: no padding in the entry: {e}")
    return e


def redirect_rip(p, insn_off, insn_len, target):
    """Points a rip-relative operand (disp32 at the end of the instruction) at target."""
    va = p.va(insn_off)
    old = bytes(p.d[insn_off + insn_len - 4:insn_off + insn_len])
    p.write(va + insn_len - 4, struct.pack("<i", target - (va + insn_len)), expect=old)


def mark_asm(reg, data):
    """reg (32-bit) = "MG" << 16 | (now + INTERVAL) * 16, clamped to 16 bits; uses xmm1, xmm8 = run clock."""
    return f"""
        movss xmm1, xmm8
        addss xmm1, dword ptr [{data} + 12]
        mulss xmm1, dword ptr [{data} + 16]
        cvttss2si {reg}, xmm1
        cmp {reg}, 0xffff
        jbe mark_fits
        mov {reg}, 0xffff
    mark_fits:
        or {reg}, {MARK << 16:#x}
    """


def patch(p):
    data = p.add_data(len(DATA), DATA)

    # 1. slower magnet pull and slower stock in-radius flights: speeds from our data block
    consts = one(p, MAGNET_CONSTS, "magnet speed loads")
    redirect_rip(p, consts.start(), 9, data + 0)
    redirect_rip(p, consts.start() + 9, 9, data + 4)
    tick_consts = one(p, TICK_CONSTS, "tick speed loads")
    redirect_rip(p, tick_consts.start() + 9, 9, data + 0)  # xmm11: stale wisps
    redirect_rip(p, tick_consts.start() + 18, 9, data + 52)  # xmm12: stock speed
    redirect_rip(p, tick_consts.start() + 27, 9, data + 56)  # xmm14: minimum flight
    magnet = one(p, MAGNET_LOOP, "magnet entry loop", consts.end(), consts.end() + 0x80)
    mark = one(p, MAGNET_MARK, "magnet flying store", magnet.end(), magnet.end() + 0x100)
    mark_va = p.va(mark.start())
    p.check(MAGNET_FLIGHT in p.d[magnet.end():mark.start()], "magnet homing: magnet flight is not dist / xmm9, xmm10")

    tick = one(p, TICK, "tick pickup loop")
    hook = tick.start(3)
    hook_va = p.va(hook)
    back = hook_va + 10
    skip = back + struct.unpack("<i", tick.group(5))[0]
    stale = one(p, TICK_STALE, "tick stale check", hook - 0x100, hook)
    push = one(p, TICK_PUSH, "tick start-flying tail", hook, hook + 0x100)
    tail_va = p.va(push.start())
    push_va = p.va(push.start(3))
    flag = one(p, LOOP_FLAG, "loop flag store", hook, push.start())
    e = entry_layout(p, magnet, mark, tick, stale, push)
    p.check(bytes.fromhex("488d48") + bytes([e["pos"]]) in p.d[flag.end():push.start()],
            "magnet homing: no lea rcx, [rax+pos]")
    # flight speed: xmm11 for stale wisps, else the stock speed register (movaps xmm0, xmm11 / xmm12 or xmm13)
    stock_mov = bytes([0x41, 0x0f, 0x28, 0xc0 | tick_consts.group(1)[0] >> 3 & 7])
    p.check(bytes.fromhex("410f28c3") in p.d[flag.end():push.start()]
            and stock_mov in p.d[flag.end():push.start()], "magnet homing: tick flight speeds moved")
    # the tail computes flight = max(dist / speed, xmm14) in xmm15 and arrival = xmm8 + xmm15
    p.check(p.d[push.start() - 14:push.start()] == bytes.fromhex("f3450f5ffe410f28c0f3410f58c7"),
            "magnet homing: start-flying tail changed")
    # both functions read the run clock from the same field into xmm8
    # (movss xmm8, [reg + disp32]; the base register differs between the two and between builds)
    xmm8_load = rb"\xf3[\x44\x45]\x0f\x10[\x80-\x83\x85-\x87]"
    clock = re.search(xmm8_load + rb"(.{4})", p.d[consts.start() - 0x80:consts.start()], re.S)
    p.check(clock is not None and re.search(xmm8_load + re.escape(clock.group(1)),
                                            p.d[hook - 0x400:hook]) is not None,
            "magnet homing: run clock not in xmm8")

    # 2. the magnet marks each wisp it pulls: rcx and rax are dead here, xmm8 = run clock
    def build_mark(va):
        return p.asm(f"""
            lea rcx, [rip + {data - (va + 7):#x}]
            mov dword ptr [rbx - {e['size'] - e['state']:#x}], 1
            {mark_asm("eax", "rcx")}
            mov dword ptr [rbx - {e['size'] - e['pad']:#x}], eax
            jmp {mark_va + 7:#x}
        """, va)

    # 3. stock start-flying tail: mark long flights so they home, clear the padding otherwise.
    # rax = entry, xmm15 = flight; rdx, r9, xmm1 are dead here
    def build_tail(va):
        return p.asm(f"""
            lea r9, [rip + {data - (va + 7):#x}]
            mov dword ptr [rax + {e['state']:#x}], 1
            xor edx, edx
            ucomiss xmm15, dword ptr [r9 + 44]
            jbe store
            {mark_asm("edx", "r9")}
        store:
            mov dword ptr [rax + {e['pad']:#x}], edx
            jmp {tail_va + 7:#x}
        """, va)

    # 4. re-aim marked flying wisps. At the hook: rax = entry, xmm7 = hero x,y, xmm8 = run clock;
    # rdx, r8, xmm0, xmm1, xmm15 are scratch (reloaded before their next use). r9 is kept: from v109
    # the loop loads the stale-guid list into it once and only the push path reloads it.
    def build_reaim(va):
        return p.asm(f"""
            push r9
            lea r9, [rip + {data - (va + 9):#x}]
            cmp byte ptr [rax + {e['state']:#x}], 0
            je stock
            cmp dword ptr [rax + {e['state']:#x}], 1
            jne pass
            mov edx, dword ptr [rax + {e['pad']:#x}]
            mov r8d, edx
            shr r8d, 16
            cmp r8d, {MARK:#x}
            jne pass
            movss xmm1, dword ptr [rax + {e['arrival']:#x}]
            subss xmm1, xmm8
            ucomiss xmm1, dword ptr [r9 + 28]
            jbe pass
            ucomiss xmm1, dword ptr [r9 + 48]
            ja pass
            movzx edx, dx
            cvtsi2ss xmm0, edx
            mulss xmm0, dword ptr [r9 + 20]
            ucomiss xmm8, xmm0
            jb pass
            ucomiss xmm8, dword ptr [r9 + 32]
            jne new_tick
            mov edx, dword ptr [r9 + 36]
            cmp edx, dword ptr [r9 + 40]
            jae pass
            jmp counted
        new_tick:
            movss dword ptr [r9 + 32], xmm8
            mov dword ptr [r9 + 36], 0
        counted:
            inc dword ptr [r9 + 36]
            movss xmm1, xmm8
            subss xmm1, xmm0
            addss xmm1, dword ptr [r9 + 12]
            movss xmm15, dword ptr [rax + {e['arrival']:#x}]
            subss xmm15, xmm0
            addss xmm15, dword ptr [r9 + 12]
            divss xmm1, xmm15
            minss xmm1, dword ptr [r9 + 24]
            maxss xmm1, dword ptr [r9 + 28]
            movsd xmm0, qword ptr [rax + {e['pos']:#x}]
            movaps xmm15, xmm7
            subps xmm15, xmm0
            shufps xmm1, xmm1, 0
            mulps xmm15, xmm1
            addps xmm0, xmm15
            movlps qword ptr [rax + {e['pos']:#x}], xmm0
            movaps xmm15, xmm7
            subps xmm15, xmm0
            mulps xmm15, xmm15
            movshdup xmm1, xmm15
            addss xmm1, xmm15
            sqrtss xmm15, xmm1
            divss xmm15, dword ptr [r9]
            maxss xmm15, dword ptr [r9 + 8]
            movss xmm1, xmm8
            addss xmm1, xmm15
            movss dword ptr [rax + {e['arrival']:#x}], xmm1
            {mark_asm("edx", "r9")}
            mov dword ptr [rax + {e['pad']:#x}], edx
            .byte {', '.join(str(b) for b in flag.group())}
            lea rcx, [rax + {e['pos']:#x}]
            pop r9
            jmp {push_va:#x}
        stock:
            pop r9
            jmp {back:#x}
        pass:
            pop r9
            jmp {skip:#x}
        """, va)

    mark_cave = p.add_cave(build_mark)
    tail_cave = p.add_cave(build_tail)
    reaim_cave = p.add_cave(build_reaim)
    p.write(mark_va, p.asm(f"jmp {mark_cave:#x}", mark_va).ljust(7, b"\x90"), expect=mark.group()[:7])
    p.write(tail_va, p.asm(f"jmp {tail_cave:#x}", tail_va).ljust(7, b"\x90"), expect=push.group()[:7])
    p.write(hook_va, p.asm(f"jmp {reaim_cave:#x}", hook_va).ljust(10, b"\x90"), expect=tick.group(3))
