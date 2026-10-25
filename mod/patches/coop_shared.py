"""Co-op shared enemies: one set of enemies for both heroes, kept in step between the two games.

Installed by coop_ghost.patch (it needs the ghost tick, the boss-sync AI hook and the hit hook);
patch() here does nothing. Meta: shared_enemies = 1, shared_rva, shared_max, shared_sig. Needs the
full sync and the boss sync (builds 107+); without them co-op keeps its own enemies on each screen.

Roles (control bits in the ghost block, set by the companion, pvp.SharedSync):
- host (LIVE without GUEST): its spawner fills the shared set as before (coop_spawns rings around
  both heroes). The companion decides who runs each enemy (the nearer hero, with hysteresis) and
  writes it back with the guest's lower health fractions; the tick sets each enemy's owner and
  deals the difference. An enemy the guest owns chases the ally and its attacks are dropped (the
  boss-sync AI hook), so the host's game never hurts the host with it.
- guest (GUEST): its own spawner is closed (spawn_enemy returns 0 unless the tick itself calls
  it). While LIVE the tick mirrors the host's table: missing ids are spawned through spawn_enemy
  (cap bypassed, guid low 24 bits = host id | MIRROR), mirrors the host dropped vanish without
  rewards, host-owned mirrors take the host's movement (a new segment and the game's own
  MONSTER_MOVE) and are lured like above, the guest's own run normally. Damage adds up: each side
  publishes its own damage so far (a fraction of max health: what its copy lost minus what it was
  dealt for the partner) and deals what the partner's grew since the last tick, once (the record
  remembers what it was dealt); a death is a kill through enemy damage (own drops and xp on each
  screen). Two heroes on one enemy kill it twice as fast, not as fast as the stronger one alone. Guest records that aren't mirrors vanish while LIVE (the solo
  enemies of a disconnect). SOLO opens the spawner again.
Shared enemies: records that are no boss, no guardian, no boss add (+0xd0 = -1) and whose template
lies in the zone's list.
Bosses and guardians can't be mirrored (spawn_enemy only makes plain enemies; their own spawners take no
index or position), so each game keeps its own copy, paired by the boss-sync key, health shared by the
boss sync. The table still carries them (id = BOSS_ID | key, template 0): the companion gives each an
owner like any enemy, and a guest's host-run boss takes the host's moves, so both screens show one boss
in one place fighting one hero. Boss adds stay with the boss sync (each game its own).

Block "AZSENMY1" (shared_rva):
  +0 magic, +8 u32 seq (exe, odd while writing), +12 u32 count, +16 f32 run clock, +20 f32 hero x, y, z,
  +32 MAX entries: my shared enemies, published every tick.
  +IN u32 seq (companion, odd while writing), u32 count, u32 flags (1: complete), u32 0, +IN+16 MAX
  entries: the partner's side (guest: the host's table; host: owners and the guest's fractions).
  Entry (ENTRY bytes): u32 id (guid & 0xffffff), u8 template index, u8 flags, u16 0, f32 x, y, z (where
  it is heading), f32 end of that move (run clock), f32 own damage so far (fraction of max health; in the
  partner's side the partner's own damage).
  Flags I publish: DEAD, ALLY_OWNS (an owner I was given: the partner's hero runs it). A boss or guardian
  has id BOSS_ID | its boss-sync key and is never spawned, killed or vanished through the table.
  Flags I take: DEAD (kill it), LURE (the partner runs it), NEW_SEG (guest: take this move), GONE
  (vanish without rewards).
A record's owner lives in the record's tail padding (never written by the game, moved with it):
+owner u8 1 = lured, +owner+1 u16 = guid & 0xffff (stale padding of another record never matches),
+owner+3 f32 the partner's damage already dealt to it (valid while the guid matches).
"""
import re
import struct
from offsets import PatchError
import zlib

MAGIC = b"AZSENMY1"
MAX, ENTRY = 200, 28
MINE, IN = 32, 32 + 200 * 28
SIZE = IN + 16 + MAX * ENTRY
DEAD, LURE, NEW_SEG, GONE = 1, 2, 4, 8
HOLD = 16  # negotiated choice shield: stop the record, neither hero owns it
ALLY_OWNS = LURE
COMPLETE = 1
LIVE, GUEST, SOLO = 16, 32, 64  # ghost block control bits
MIRROR = 0x800000
BOSS_ID = 0x1000000  # table ids of bosses and guardians: this | boss-sync key
SPAWNS_PER_TICK = 16
EPS = 0.002  # partner damage below this (fraction of max health) waits until it adds up
LEDGER_SIG = 0x5EDA0002  # in shared_sig: games that add damage never pair with ones that took the lower health
MIN_DUR = 0.05  # s: the shortest move a host segment becomes
CORPSE_AGO = 1000.0  # s: a vanished record's time of death, long past the AI's corpse delay

# private state
TPL_BASE, TPL_END, LIVE_NOW, LURE_OK, IN_SPAWN, NEW_SEQ, LAST_SEQ = 0, 8, 16, 17, 18, 19, 20
PEND_N, SPAWNED, SAVED_CTR, DUR_MS, CUR_E = 24, 28, 32, 36, 40
K_EPS, K_MIN_DUR, K_1000, K_TIMEOUT, K_AGO = 48, 52, 56, 60, 64
PKT, FROM, TO, OPT, SEEN, COPY, PEND = 96, 128, 144, 160, 256, 512, 6144
STATE_SIZE = PEND + 16 * MAX

# spawn_enemy: alive count vs the cap clamp(density / 100 * 50, 50, 120): ... cmp rcx, rdx ; jae fail
CAP = re.compile(rb"\x48\x83\xfa\x33\x41\xb8\x32\x00\x00\x00\x4c\x0f\x43\xc2\x49\x83\xf8\x78\xba\x78\x00\x00\x00"
                 rb"\x49\x0f\x42\xd0(\x48\x39\xd1\x0f\x83(....))", re.S)
# its template: mov rcx, [rsi+zone id ptr] ; mov rdx, [rsi+zone id len] ; call zone index ; imul rax, rax, stride ;
# lea rbx, [rip+zones] ; mov rdx, [rax+rbx+list len] ... lea r15, [r14+r14*4] ; shl r15, 4 ; add r15, [rbx+list]
ZONE = re.compile(rb"\x48\x8b\x8e(....)\x48\x8b\x96(....)\xe8(....)\x48\x69\xc0(....)\x48\x8d\x1d(....)\x48\x8b\x94\x18(....)", re.S)
LIST = re.compile(rb"\x4f\x8d\x3c\xb6\x49\xc1\xe7\x04\x4c\x03\x7b(.)", re.S)
TEMPLATE = 0x50
# guid = 0xF130 << 48 | entry << 24 | ++counter: mov r13d, [rsi+counter] ; inc r13d ; mov [rsi+counter], r13d
COUNTER = re.compile(rb"\x44\x8b\xae(....)\x41\xff\xc5\x44\x89\xae\1", re.S)
# the new record: boss, guardian = -1 ; dead = 0 ; ... ; add info = -1 ; guid ; template
INIT = re.compile(rb"\x48\xc7\x44\x08(.)\xff\xff\xff\xff\x48\xc7\x44\x08(.)\xff\xff\xff\xff\xc7\x84\x08(....)\x00\x00\x00\x00"
                  rb"(?:\xc7\x84\x08....\x00\x00\x00\x00)*\xc7\x84\x08(....)\xff\xff\xff\xff\x4c\x89\xa4\x08(....)"
                  rb"\x4c\x89\xbc\x08(....)", re.S)
# its last field, the elite byte, then the push: mov [rax+rcx+elite], bl ; inc rdi ; mov [rsi+len], rdi
ELITE = re.compile(rb"\x88\x9c\x08(....)\x48\xff\xc7\x48\x89\xbe(....)", re.S)
# enemy damage's death: mov dword [rcx+dead], 1 ; movss [rcx+died at], xmm8 ; mov qword [rcx+effects len], 0
DIED = rb"\xc7\x81%s\x01\x00\x00\x00\xf3\x44\x0f\x11\x81(....)\x48\xc7\x41(.)\x00\x00\x00\x00"
# the enemy AI sends its moves after the loop: mov ebx, [rax+spline] ; inc ebx ; mov [rax+spline], ebx ; ucomiss ;
# jae ; mov rcx, [rax+nav] ; add rcx, 0x10 ; movsd xmm8, [rbp+..] ; movshdup xmm2, xmm8 ; movaps xmm1, xmm8 ;
# call ground height ... mov [rsp+0x28], ecx (ms) ; mov [rsp+0x20], ebx ; mov rcx, rdi ; mov rdx, r13 (guid) ;
# lea r8, [rbp+from] ; mov r9, r12 (to) ; call SMSG_MONSTER_MOVE builder
SPLINE = re.compile(rb"\x8b\x98(....)\xff\xc3\x89\x98\1\x45\x0f\x2e\xd4\x0f\x83....\x48\x8b\x88(....)\x48\x83\xc1\x10"
                    rb"\xf2\x44\x0f\x10\x85....\xf3\x41\x0f\x16\xd0\x41\x0f\x28\xc8\xe8(....)", re.S)
BUILD = re.compile(rb"\x89\x4c\x24\x28\x89\x5c\x24\x20\x48\x89\xf9\x4c\x89\xea\x4c\x8d\x45.\x4d\x89\xe1\xe8(....)", re.S)
PROLOGUE = b"\x55\x41\x57\x41\x56"  # push rbp ; push r15 ; push r14


def s32(b):
    return struct.unpack("<i", b)[0]


def patch(p):
    """Nothing: coop_ghost.patch installs the shared enemies (see install)."""


def find(p, full, boss, enemy_damage, func_range):
    """Everything the shared enemies hook or call, read from the game code and cross-checked against
    the full sync and boss sync layouts; raises PatchError when the game changed."""
    caps = list(CAP.finditer(p.d))
    p.check(len(caps) == 1, f"shared enemies: {len(caps)} spawn caps")
    cap = caps[0]
    spawn_lo, spawn_hi = func_range(p, p.va(cap.start()))
    lo, hi = p.off(spawn_lo), p.off(spawn_hi)
    p.check(p.d[lo:lo + 5] == PROLOGUE, "shared enemies: spawn_enemy prologue changed")
    found = [list(rx.finditer(p.d, lo, hi)) for rx in (ZONE, LIST, COUNTER, INIT, ELITE)]
    p.check(all(len(f) == 1 for f in found), f"shared enemies: spawner code {[len(f) for f in found]}")
    zone, tlist, counter, init, elite = (f[0] for f in found)
    boss_off, guard_off, dead, add, guid, tpl = (init.group(1)[0], init.group(2)[0], *(s32(init.group(i)) for i in range(3, 7)))
    owner = s32(elite.group(1)) + 1
    p.check(boss_off == boss["boss"] and guard_off == boss["guard"] and dead == boss["dead"] and guid == full["guid"]
            and s32(elite.group(2)) == full["enemies"] + 8,
            "shared enemies: record layout disagrees")
    # owner, guid and the partner's dealt damage: 3 pad bytes before build 109 (no room: no shared enemies), 7 after
    p.check(owner + 7 <= full["size"], "shared enemies: no padding for the damage ledger")
    ed_lo, ed_hi = (p.off(v) for v in func_range(p, enemy_damage))
    died = list(re.finditer(DIED % re.escape(struct.pack("<i", dead)), p.d[ed_lo:ed_hi], re.S))
    p.check(len(died) == 1, f"shared enemies: {len(died)} deaths in enemy damage")
    died_at, effects = s32(died[0].group(1)), died[0].group(2)[0]
    ai_lo, ai_hi = (p.off(v) for v in func_range(p, boss["loop"]))
    corpse = b"\xf3\x41\x0f\x5c\x80" + struct.pack("<i", died_at)  # subss xmm0, [r8+died at] at the loop head
    sends = list(SPLINE.finditer(p.d, ai_lo, ai_hi))
    p.check(corpse in p.d[p.off(boss["loop"]):p.off(boss["loop"]) + 0x80] and len(sends) == 1,
            "shared enemies: AI corpse check or move sender not found")
    send = sends[0]
    build = BUILD.search(p.d, send.end(), send.end() + 0x100)
    p.check(build is not None, "shared enemies: move builder not found")
    zones = p.va(zone.end(5)) + s32(zone.group(5))
    stride, length, tlist_off = s32(zone.group(4)), s32(zone.group(6)), tlist.group(1)[0]
    p.check(length == tlist_off + 8, "shared enemies: zone list disagrees")
    return {"boss": boss_off, "guard": guard_off, "dead": dead, "max": boss["max"], "spawn": spawn_lo, "cap": p.va(cap.start(1)), "cap_bytes": cap.group(1),
            "cap_fail": p.va(cap.end(1)) + s32(cap.group(2)), "zone_args": (s32(zone.group(1)), s32(zone.group(2))),
            "zone_fn": p.va(zone.end(3)) + s32(zone.group(3)), "zones": zones, "stride": stride, "list": tlist_off,
            "counter": s32(counter.group(1)), "add": add, "tpl": tpl, "owner": owner, "died_at": died_at,
            "effects": effects, "spline": s32(send.group(1)), "nav": s32(send.group(2)),
            "ground": p.va(send.end()) + s32(send.group(3)), "builder": p.va(build.end()) + s32(build.group(1)),
            "sig": template_sig(p, zones, stride, tlist_off) ^ LEDGER_SIG}


def template_sig(p, zones, stride, tlist):
    """crc32 of every zone's enemy template list as the exe file holds it: two games mirror each other's
    enemies by template index only when this matches."""
    blob = b""
    p.offsets.symbol('shared.zone_table', zones, kind='data', evidence='zone list and spawner cross-checks')
    for i in range(32):
        entry = zones + i * stride + tlist
        try:
            n = struct.unpack('<Q', p.offsets.read(entry + 8, 8))[0]
            if not 0 < n <= 64:
                break
            ptr = p.offsets.follow_pointer(f'shared.zone.{i}.templates', entry)
            rows = p.offsets.read(ptr, n * TEMPLATE)
        except (PatchError, ValueError, struct.error):
            break
        blob += rows
    p.check(len(blob) >= 12 * TEMPLATE, "shared enemies: zone template lists not found")
    return zlib.crc32(blob)


def is_shared(rec, s, base, no, tmp="rdx"):
    """Jumps to `no` unless the record at `rec` is a shared enemy (base: the shared state, whose template
    range the tick refreshes)."""
    return f"""
        cmp qword ptr [{rec} + {s['boss']:#x}], -1
        jne {no}
        cmp qword ptr [{rec} + {s['guard']:#x}], -1
        jne {no}
        cmp dword ptr [{rec} + {s['add']:#x}], -1
        jne {no}
        mov {tmp}, qword ptr [{rec} + {s['tpl']:#x}]
        cmp {tmp}, qword ptr [{base} + {TPL_BASE}]
        jb {no}
        cmp {tmp}, qword ptr [{base} + {TPL_END}]
        jae {no}
    """


def install(p, s, g):
    """The shared enemies: data, the spawn gate and cap bypass, the tick routine (returned: the ghost tick
    calls it with rcx = run state) and the replay filter (s["skip"]). g: coop_ghost's addresses and layout."""
    eb = p.add_data(SIZE, MAGIC)
    sh = p.add_data(STATE_SIZE)
    at = sh - p.data_va + K_EPS
    p.data[at:at + 20] = struct.pack("<5f", EPS, MIN_DUR, 1000.0, g["timeout"], CORPSE_AGO)
    s.update(eb=eb, sh=sh)
    p.meta.update(shared_enemies=1, shared_rva=eb - p.ib, shared_max=MAX, shared_sig=s["sig"])
    control = g["block"] + g["B_CONTROL"]
    full = g["full"]

    def gate(va):
        # spawn_enemy entry: closed on the guest unless SOLO or the tick's own mirror spawn
        return p.asm(f"""
            lea rax, [rip + {control - (va + 7):#x}]
            test byte ptr [rax], {GUEST}
            jz go
            test byte ptr [rax], {SOLO}
            jnz go
            cmp byte ptr [rax + {sh + IN_SPAWN - control:#x}], 0
            jne go
            xor eax, eax
            ret
        go:
            push rbp
            push r15
            push r14
            jmp {s['spawn'] + len(PROLOGUE):#x}
        """, va)
    cave = p.add_cave(gate)
    p.write(s["spawn"], p.asm(f"jmp {cave:#x}", s["spawn"]), expect=PROLOGUE)

    def bypass(va):
        back = s["cap"] + len(s["cap_bytes"])
        return p.asm(f"""
            cmp byte ptr [rip + {sh + IN_SPAWN - (va + 7):#x}], 0
            jne ok
            cmp rcx, rdx
            jae {s['cap_fail']:#x}
        ok:
            jmp {back:#x}
        """, va)
    cave = p.add_cave(bypass)
    p.write(s["cap"], p.asm(f"jmp {cave:#x}", s["cap"]).ljust(len(s["cap_bytes"]), b"\x90"), expect=s["cap_bytes"])

    def skip(va):
        # (rcx run state, rdx guid) -> al = 1 when that enemy is shared and shared mode is on (replays skip it)
        return p.asm(f"""
            lea r8, [rip + {control - (va + 7):#x}]
            xor eax, eax
            test byte ptr [r8], {LIVE | GUEST}
            jz done
            lea r8, [r8 + {sh - control:#x}]
            mov r9, qword ptr [rcx + {full['enemies']:#x}]
            mov r10, qword ptr [rcx + {full['enemies'] + 8:#x}]
        find:
            test r10, r10
            jz done
            cmp qword ptr [r9 + {full['guid']:#x}], rdx
            je found
            add r9, {full['size']:#x}
            dec r10
            jmp find
        found:
            {is_shared('r9', s, 'r8', 'done', 'r11')}
            mov al, 1
        done:
            ret
        """, va)
    s["skip"] = p.add_cave(skip)
    tick = p.add_cave(lambda va: p.asm(tick_code(s, g, eb, sh, va), va))
    p.log.append(f"coop: shared enemies, spawn {s['spawn']:x} (cap {s['cap']:x}, counter {s['counter']:#x}), "
                 f"template sig {s['sig']:08x}, mover {s['builder']:x}")
    return tick


def tick_code(s, g, eb, sh, va):
    """The tick routine (rcx = run state): publish my shared enemies, then while LIVE apply the partner's
    table (owners, fractions, kills, vanishing, the guest's mirrors and their moves) and deal the damage.
    r15 = shared state, r14 = ghost state, r12 = AZSENMY1, rbp = ghost block, r13 = run state."""
    full, gs, block = g["full"], g["state"], g["block"]
    size, guid, dead = full["size"], full["guid"], s["dead"]
    hp, mx, own = full["hp"], s["max"], s["owner"]
    acc = own + 3  # the partner's damage dealt to the record so far (fraction of its max health)
    lerp_axis = "".join(f"""
            movss xmm1, dword ptr [rsi + {full['to'] + 4 * k:#x}]
            subss xmm1, dword ptr [rsi + {full['from'] + 4 * k:#x}]
            mulss xmm1, xmm0
            addss xmm1, dword ptr [rsi + {full['from'] + 4 * k:#x}]
            movss dword ptr [r15 + {FROM + 4 * k}], xmm1
    """ for k in range(3))
    ground = lambda at, tag: f"""
            mov rcx, qword ptr [r13 + {s['nav']:#x}]
            add rcx, 0x10
            movss xmm1, dword ptr [r15 + {at}]
            movss xmm2, dword ptr [r15 + {at + 4}]
            call {s['ground']:#x}
            test al, 1
            jz {tag}
            movss dword ptr [r15 + {at + 8}], xmm0
        {tag}:
    """
    vanish = f"""
            mov dword ptr [rsi + {dead:#x}], 1
            movss xmm0, dword ptr [r13 + {g['clock']:#x}]
            subss xmm0, dword ptr [r15 + {K_AGO}]
            movss dword ptr [rsi + {s['died_at']:#x}], xmm0
            mov qword ptr [rsi + {s['effects']:#x}], 0
    """
    secondary = f"""
            mov byte ptr [r14 + {g['REPLAYING']}], 1
            mov byte ptr [r14 + {g['OVERRIDE']}], 1
            movzx eax, byte ptr [r13 + {full['kind']:#x}]
            mov byte ptr [r14 + {g['SAVED_KIND']}], al
            mov byte ptr [r13 + {full['kind']:#x}], 3
            mov qword ptr [rsp + 0x20], 0
            mov qword ptr [rsp + 0x28], 0
            mov qword ptr [rsp + 0x30], 0
            mov rcx, r13
            mov rdx, qword ptr [r14 + {g['TARGET']}]
            movss xmm2, dword ptr [r14 + {g['REPLAY_DMG']}]
            mov r9d, dword ptr [r14 + {g['SPELL_ID']}]
            call {g['enemy_damage']:#x}
            movzx eax, byte ptr [r14 + {g['SAVED_KIND']}]
            mov byte ptr [r13 + {full['kind']:#x}], al
            mov byte ptr [r14 + {g['REPLAYING']}], 0
            mov byte ptr [r14 + {g['OVERRIDE']}], 0
    """
    return f"""
            lea rax, [rip + {sh - (va + 7):#x}]
            push rbx
            push rbp
            push rsi
            push rdi
            push r12
            push r13
            push r14
            push r15
            sub rsp, 0x68
            mov r13, rcx
            mov r15, rax
            lea r12, [r15 + {eb - sh:#x}]
            lea r14, [r15 + {gs - sh:#x}]
            lea rbp, [r15 + {block - sh:#x}]

            mov rcx, qword ptr [r13 + {s['zone_args'][0]:#x}]
            mov rdx, qword ptr [r13 + {s['zone_args'][1]:#x}]
            call {s['zone_fn']:#x}
            imul rax, rax, {s['stride']:#x}
            lea rcx, [r15 + {s['zones'] - sh:#x}]
            add rcx, rax
            mov rax, qword ptr [rcx + {s['list']:#x}]
            mov qword ptr [r15 + {TPL_BASE}], rax
            mov rdx, qword ptr [rcx + {s['list'] + 8:#x}]
            imul rdx, rdx, {TEMPLATE}
            add rdx, rax
            mov qword ptr [r15 + {TPL_END}], rdx

            mov byte ptr [r15 + {LIVE_NOW}], 0
            mov byte ptr [r15 + {LURE_OK}], 0
            test byte ptr [rbp + {g['B_CONTROL']}], {LIVE}
            jz publish
            cmp byte ptr [r13 + {g['phase']:#x}], 1
            jne publish
            mov byte ptr [r15 + {LIVE_NOW}], 1
            cmp dword ptr [r14 + {g['SPAWNED']}], 0
            je publish
            cmp byte ptr [r14 + {g['ALLY_DEAD']}], 0
            jne publish
            movss xmm0, dword ptr [r14 + {g['CLOCK_NOW']}]
            subss xmm0, dword ptr [r14 + {g['LAST_UPD']}]
            ucomiss xmm0, dword ptr [r15 + {K_TIMEOUT}]
            ja publish
            mov byte ptr [r15 + {LURE_OK}], 1

        publish:
            inc dword ptr [r12 + 8]
            xor ebx, ebx
            lea rdi, [r12 + {MINE}]
            mov rsi, qword ptr [r13 + {full['enemies']:#x}]
            mov r8, qword ptr [r13 + {full['enemies'] + 8:#x}]
        p_rec:
            test r8, r8
            jz p_end
            {is_shared('rsi', s, 'r15', 'p_boss')}
            cmp ebx, {MAX}
            jae p_end
            mov eax, dword ptr [rsi + {guid:#x}]
            and eax, 0xffffff
            mov dword ptr [rdi], eax
            mov rax, qword ptr [rsi + {s['tpl']:#x}]
            sub rax, qword ptr [r15 + {TPL_BASE}]
            xor edx, edx
            mov ecx, {TEMPLATE}
            div rcx
            movzx eax, al
            jmp p_common
        p_boss:
            call boss_key
            test eax, eax
            jz p_next
            cmp ebx, {MAX}
            jae p_end
            mov dword ptr [rdi], eax
            xor eax, eax
        p_common:
            xor ecx, ecx
            cmp byte ptr [rsi + {dead:#x}], 0
            je p_alive
            or ecx, {DEAD}
        p_alive:
            mov dx, word ptr [rsi + {guid:#x}]
            cmp dx, word ptr [rsi + {own + 1:#x}]
            jne p_flags
            test byte ptr [rsi + {own:#x}], 2
            jz p_owner
            or ecx, {HOLD}
            jmp p_flags
        p_owner:
            test byte ptr [rsi + {own:#x}], 1
            jz p_flags
            or ecx, {ALLY_OWNS}
        p_flags:
            shl ecx, 8
            or eax, ecx
            mov dword ptr [rdi + 4], eax
            mov rax, qword ptr [rsi + {full['to']:#x}]
            mov qword ptr [rdi + 8], rax
            mov eax, dword ptr [rsi + {full['to'] + 8:#x}]
            mov dword ptr [rdi + 16], eax
            movss xmm0, dword ptr [rsi + {full['start']:#x}]
            addss xmm0, dword ptr [rsi + {full['dur']:#x}]
            movss dword ptr [rdi + 20], xmm0
            xorps xmm0, xmm0
            cmp byte ptr [rsi + {dead:#x}], 0
            jne p_frac
            movss xmm1, dword ptr [rsi + {mx:#x}]
            ucomiss xmm1, xmm0
            jbe p_frac
            movss xmm0, dword ptr [rsi + {hp:#x}]
            divss xmm0, xmm1
            xorps xmm1, xmm1
            maxss xmm0, xmm1
        p_frac:
            mov eax, 0x3f800000
            movd xmm1, eax
            subss xmm1, xmm0
            mov dx, word ptr [rsi + {guid:#x}]
            cmp dx, word ptr [rsi + {own + 1:#x}]
            jne p_own
            subss xmm1, dword ptr [rsi + {acc:#x}]
        p_own:
            xorps xmm0, xmm0
            maxss xmm1, xmm0
            movss dword ptr [rdi + 24], xmm1
            inc ebx
            add rdi, {ENTRY}
        p_next:
            add rsi, {size:#x}
            dec r8
            jmp p_rec
        p_end:
            mov dword ptr [r12 + 12], ebx
            mov eax, dword ptr [r13 + {g['clock']:#x}]
            mov dword ptr [r12 + 16], eax
            mov rax, qword ptr [r13 + {g['hero']:#x}]
            mov qword ptr [r12 + 20], rax
            mov eax, dword ptr [r13 + {g['hero'] + 8:#x}]
            mov dword ptr [r12 + 28], eax
            inc dword ptr [r12 + 8]

            cmp byte ptr [r15 + {LIVE_NOW}], 0
            je done

            mov eax, dword ptr [r12 + {IN}]
            test al, 1
            jnz done
            lea rsi, [r12 + {IN}]
            lea rdi, [r15 + {COPY}]
            mov ecx, {16 + MAX * ENTRY}
            rep movsb
            cmp eax, dword ptr [r12 + {IN}]
            jne done
            mov byte ptr [r15 + {NEW_SEQ}], 0
            cmp eax, dword ptr [r15 + {LAST_SEQ}]
            je a_same
            mov dword ptr [r15 + {LAST_SEQ}], eax
            mov byte ptr [r15 + {NEW_SEQ}], 1
        a_same:
            cmp dword ptr [r15 + {COPY + 4}], {MAX}
            jbe a_count
            mov dword ptr [r15 + {COPY + 4}], {MAX}
        a_count:
            lea rdi, [r15 + {SEEN}]
            mov ecx, {MAX}
            xor eax, eax
            rep stosb
            mov dword ptr [r15 + {PEND_N}], 0
            mov rsi, qword ptr [r13 + {full['enemies']:#x}]
            mov rbx, qword ptr [r13 + {full['enemies'] + 8:#x}]
        a_rec:
            test rbx, rbx
            jz a_spawns
            {is_shared('rsi', s, 'r15', 'a_boss')}
            mov eax, dword ptr [rsi + {guid:#x}]
            and eax, 0xffffff
            test byte ptr [rbp + {g['B_CONTROL']}], {GUEST}
            jz a_key
            test eax, {MIRROR}
            jz a_vanish
            and eax, {MIRROR - 1}
        a_key:
            call lookup
            test rdx, rdx
            jnz a_found
        a_missing:
            test byte ptr [rbp + {g['B_CONTROL']}], {GUEST}
            jz a_next
            test byte ptr [r15 + {COPY + 8}], {COMPLETE}
            jz a_next
            jmp a_vanish
        a_found:
            mov byte ptr [r15 + rcx + {SEEN}], 1
            cmp byte ptr [rsi + {dead:#x}], 0
            jne a_next
            mov qword ptr [r15 + {CUR_E}], rdx
            mov cx, word ptr [rsi + {guid:#x}]
            cmp cx, word ptr [rsi + {own + 1:#x}]
            je a_ledger
            mov dword ptr [rsi + {acc:#x}], 0
        a_ledger:
            movzx eax, byte ptr [rdx + 5]
            mov ecx, eax
            shr ecx, 1
            and ecx, 1
            test eax, {HOLD}
            jz a_owner_ready
            mov ecx, 2
            push rdx
            mov dx, word ptr [rsi + {guid:#x}]
            cmp dx, word ptr [rsi + {own + 1:#x}]
            pop rdx
            jne a_owner_ready
            test byte ptr [rsi + {own:#x}], 2
            jz a_owner_ready
            mov cl, byte ptr [rsi + {own:#x}]
        a_owner_ready:
            mov byte ptr [rsi + {own:#x}], cl
            mov cx, word ptr [rsi + {guid:#x}]
            mov word ptr [rsi + {own + 1:#x}], cx
            test eax, {GONE}
            jnz a_vanish
            test eax, {DEAD}
            jnz a_kill
            xorps xmm0, xmm0
            movss xmm1, dword ptr [rsi + {mx:#x}]
            ucomiss xmm1, xmm0
            jbe a_move
            movss xmm2, dword ptr [rdx + 24]
            subss xmm2, dword ptr [rsi + {acc:#x}]
            ucomiss xmm2, dword ptr [r15 + {K_EPS}]
            jbe a_move
            cmp dword ptr [r15 + {PEND_N}], {MAX}
            jae a_move
            movss xmm0, dword ptr [rdx + 24]
            movss dword ptr [rsi + {acc:#x}], xmm0
            mulss xmm2, xmm1
            movaps xmm0, xmm2
            call a_pend
            jmp a_move
        a_kill:
            movss xmm0, dword ptr [rsi + {hp:#x}]
            addss xmm0, dword ptr [rsi + {mx:#x}]
            call a_pend
            jmp a_next
        a_move:
            test byte ptr [rsi + {own:#x}], 2
            jnz a_next
            test byte ptr [rbp + {g['B_CONTROL']}], {GUEST}
            jz a_next
            cmp byte ptr [r15 + {NEW_SEQ}], 0
            je a_next
            mov rdx, qword ptr [r15 + {CUR_E}]
            movzx eax, byte ptr [rdx + 5]
            and eax, {LURE | NEW_SEG}
            cmp eax, {LURE | NEW_SEG}
            jne a_next
            movss xmm0, dword ptr [r13 + {g['clock']:#x}]
            subss xmm0, dword ptr [rsi + {full['start']:#x}]
            movss xmm1, dword ptr [rsi + {full['dur']:#x}]
            xorps xmm2, xmm2
            ucomiss xmm1, xmm2
            jbe m_end
            divss xmm0, xmm1
            maxss xmm0, xmm2
            mov eax, 0x3f800000
            movd xmm1, eax
            minss xmm0, xmm1
            jmp m_lerp
        m_end:
            mov eax, 0x3f800000
            movd xmm0, eax
        m_lerp:
            {lerp_axis}
            mov rax, qword ptr [rdx + 8]
            mov qword ptr [r15 + {TO}], rax
            mov eax, dword ptr [rdx + 16]
            mov dword ptr [r15 + {TO + 8}], eax
            mov rax, qword ptr [r15 + {FROM}]
            mov qword ptr [rsi + {full['from']:#x}], rax
            mov eax, dword ptr [r15 + {FROM + 8}]
            mov dword ptr [rsi + {full['from'] + 8:#x}], eax
            mov rax, qword ptr [r15 + {TO}]
            mov qword ptr [rsi + {full['to']:#x}], rax
            mov eax, dword ptr [r15 + {TO + 8}]
            mov dword ptr [rsi + {full['to'] + 8:#x}], eax
            movss xmm0, dword ptr [rdx + 20]
            subss xmm0, dword ptr [r13 + {g['clock']:#x}]
            maxss xmm0, dword ptr [r15 + {K_MIN_DUR}]
            movss dword ptr [rsi + {full['dur']:#x}], xmm0
            mov eax, dword ptr [r13 + {g['clock']:#x}]
            mov dword ptr [rsi + {full['start']:#x}], eax
            mulss xmm0, dword ptr [r15 + {K_1000}]
            cvttss2si eax, xmm0
            mov dword ptr [r15 + {DUR_MS}], eax
            {ground(FROM, 'm_g1')}
            {ground(TO, 'm_g2')}
            inc dword ptr [r13 + {s['spline']:#x}]
            mov eax, dword ptr [r13 + {s['spline']:#x}]
            mov dword ptr [rsp + 0x20], eax
            mov eax, dword ptr [r15 + {DUR_MS}]
            mov dword ptr [rsp + 0x28], eax
            lea rcx, [r15 + {PKT}]
            mov rdx, qword ptr [rsi + {guid:#x}]
            lea r8, [r15 + {FROM}]
            lea r9, [r15 + {TO}]
            call {s['builder']:#x}
            lea rcx, [r13 + {g['queue']:#x}]
            mov rax, qword ptr [rcx + 0x10]
            cmp rax, qword ptr [rcx]
            jne m_room
            call {g['grow']:#x}
            lea rcx, [r13 + {g['queue']:#x}]
            mov rax, qword ptr [rcx + 0x10]
        m_room:
            shl rax, 5
            add rax, qword ptr [rcx + 8]
            movups xmm0, xmmword ptr [r15 + {PKT}]
            movups xmmword ptr [rax], xmm0
            movups xmm0, xmmword ptr [r15 + {PKT + 16}]
            movups xmmword ptr [rax + 0x10], xmm0
            inc qword ptr [rcx + 0x10]
            jmp a_next
        a_boss:
            cmp byte ptr [rsi + {dead:#x}], 0
            jne a_next
            call boss_key
            test eax, eax
            jz a_next
            call lookup
            test rdx, rdx
            jz a_next
            mov qword ptr [r15 + {CUR_E}], rdx
            movzx eax, byte ptr [rdx + 5]
            shr eax, 1
            and eax, 1
            mov byte ptr [rsi + {own:#x}], al
            mov ax, word ptr [rsi + {guid:#x}]
            mov word ptr [rsi + {own + 1:#x}], ax
            jmp a_move
        a_vanish:
            cmp byte ptr [rsi + {dead:#x}], 0
            jne a_next
            {vanish}
        a_next:
            add rsi, {size:#x}
            dec rbx
            jmp a_rec

        a_spawns:
            test byte ptr [rbp + {g['B_CONTROL']}], {GUEST}
            jz deal
            mov dword ptr [r15 + {SPAWNED}], 0
            xor ebx, ebx
        s_entry:
            cmp ebx, dword ptr [r15 + {COPY + 4}]
            jae deal
            cmp byte ptr [r15 + rbx + {SEEN}], 0
            jne s_next
            imul eax, ebx, {ENTRY}
            lea rsi, [r15 + rax + {COPY + 16}]
            test byte ptr [rsi + 5], {DEAD | GONE}
            jnz s_next
            test dword ptr [rsi], {BOSS_ID}
            jnz s_next
            movzx eax, byte ptr [rsi + 4]
            imul eax, eax, {TEMPLATE}
            add rax, qword ptr [r15 + {TPL_BASE}]
            cmp rax, qword ptr [r15 + {TPL_END}]
            jae s_next
            cmp dword ptr [r15 + {SPAWNED}], {SPAWNS_PER_TICK}
            jae deal
            inc dword ptr [r15 + {SPAWNED}]
            mov eax, dword ptr [r13 + {s['counter']:#x}]
            mov dword ptr [r15 + {SAVED_CTR}], eax
            mov eax, dword ptr [rsi]
            or eax, {MIRROR}
            dec eax
            mov dword ptr [r13 + {s['counter']:#x}], eax
            mov dword ptr [r15 + {OPT}], 1
            mov rax, qword ptr [rsi + 8]
            mov qword ptr [r15 + {OPT + 4}], rax
            mov eax, dword ptr [rsi + 16]
            mov dword ptr [r15 + {OPT + 12}], eax
            mov byte ptr [r15 + {IN_SPAWN}], 1
            mov rcx, r13
            movzx edx, byte ptr [rsi + 4]
            lea r8, [r15 + {OPT}]
            call {s['spawn']:#x}
            mov byte ptr [r15 + {IN_SPAWN}], 0
            mov ecx, dword ptr [r15 + {SAVED_CTR}]
            mov dword ptr [r13 + {s['counter']:#x}], ecx
            cmp rax, 1
            jne s_next
            mov rax, qword ptr [r13 + {full['enemies'] + 8:#x}]
            dec rax
            imul rax, rax, {size:#x}
            add rax, qword ptr [r13 + {full['enemies']:#x}]
            movzx ecx, byte ptr [rsi + 5]
            shr ecx, 1
            and ecx, 1
            mov byte ptr [rax + {own:#x}], cl
            mov cx, word ptr [rax + {guid:#x}]
            mov word ptr [rax + {own + 1:#x}], cx
            mov dword ptr [rax + {acc:#x}], 0
        s_next:
            inc ebx
            jmp s_entry

        deal:
            mov ecx, dword ptr [r14 + {g['B_CLASS']}]
            cmp ecx, 11
            jbe d_class
            xor ecx, ecx
        d_class:
            lea rdx, [r15 + {g['spells'] - sh:#x}]
            mov eax, dword ptr [rdx + rcx * 8]
            mov dword ptr [r14 + {g['SPELL_ID']}], eax
            xor ebx, ebx
        d_next:
            cmp ebx, dword ptr [r15 + {PEND_N}]
            jae done
            mov eax, ebx
            shl eax, 4
            lea rsi, [r15 + rax + {PEND}]
            mov rax, qword ptr [rsi]
            mov qword ptr [r14 + {g['TARGET']}], rax
            mov eax, dword ptr [rsi + 8]
            mov dword ptr [r14 + {g['REPLAY_DMG']}], eax
            {secondary}
            inc ebx
            jmp d_next

        lookup:
            xor ecx, ecx
            lea rdx, [r15 + {COPY + 16}]
        l_entry:
            cmp ecx, dword ptr [r15 + {COPY + 4}]
            jae l_none
            cmp dword ptr [rdx], eax
            je l_done
            inc ecx
            add rdx, {ENTRY}
            jmp l_entry
        l_none:
            xor edx, edx
        l_done:
            ret

        boss_key:
            mov rax, qword ptr [rsi + {guid:#x}]
            xor ecx, ecx
        k_slot:
            cmp rax, qword ptr [r14 + rcx * 8 + {g['BOSS_GUIDS']}]
            je k_found
            inc ecx
            cmp ecx, {g['BOSS_SLOTS']}
            jb k_slot
            xor eax, eax
            ret
        k_found:
            mov eax, dword ptr [r14 + rcx * 4 + {g['BOSS_KEYS']}]
            test eax, eax
            jz k_none
            or eax, {BOSS_ID}
        k_none:
            ret

        a_pend:
            mov ecx, dword ptr [r15 + {PEND_N}]
            cmp ecx, {MAX}
            jae a_pended
            shl ecx, 4
            lea rcx, [r15 + rcx + {PEND}]
            mov rax, qword ptr [rsi + {guid:#x}]
            mov qword ptr [rcx], rax
            movss dword ptr [rcx + 8], xmm0
            inc dword ptr [r15 + {PEND_N}]
        a_pended:
            ret

        done:
            add rsp, 0x68
            pop r15
            pop r14
            pop r13
            pop r12
            pop rdi
            pop rsi
            pop rbp
            pop rbx
            ret
    """
