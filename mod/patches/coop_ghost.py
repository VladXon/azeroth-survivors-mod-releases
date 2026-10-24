"""Co-op partner: the partner's hero runs around on your map in real time.

The figure exists only on the client: the run server never learns about it, so enemies
can't target it, it can't be hit, it never picks anything up and never counts as a kill.
Each run-server tick a cave reads the partner state the companion (pvp.py) writes into a
.moddata block and pushes ready-made packets into the server's outbound queue
(Vec<Packet> found by find_queue, state+0x5d0 in build 105 and +0x618 in 107, 32-byte entries
{cap, ptr, len, opcode}):
  SMSG_UPDATE_OBJECT (create), SMSG_MONSTER_MOVE (spline to the new position over about the
  update interval, run mode), SMSG_DESTROY_OBJECT.
The byte layouts mirror the game's own encoders (build 105: 0x1458a7bf0 create block, 0x1458a83c0
update wrap, 0x1458aa9e0 monster move, 0x1458ac240 destroy; unchanged in 107 at 0x1458a89a0,
0x1458a9170, 0x1458c2460, 0x1458c3e10).

Ally (default): a player object (type 4) with the partner's race, gender, skin/face/hair and
visible gear, selectable, named by the partner's nick: the server's name-query handler answers
every guid with the hero's own name, so a second cave makes it answer the ally guid with the
nick from the block. Ghost (fallback when the name-query handler isn't found): a plain,
unselectable creature with a player display.

Companion contract, block "AZSGHST3" (rva in feed json as ghost_rva), seqlock on seq:
  +0 magic[8], +8 u32 seq (odd while writing), +12 u32 active, +16 f32 x, y, z, facing,
  +32 u32 race, class, gender, display_hint (ghost only), +48 u8 skin, face, hair_style,
  hair_color, facial_hair, +56 u32 level, +60 f32 dps, +64 u32 flags (2 dead, 4 paused, 8 the
  partner streams real hits: replay them, no dps estimate), +68 u32 spell_hint,
  +72 u32 items[19] (by equipment slot), +148 u8 nick[16] (utf-8), +164 u32 summon_count,
  +168 summons[4] {u32 kind, f32 dx, dy}, +216 u32 control, +220 u8 lang (0 English,
  1 Russian), +224 u32 hp, max hp (0: keep 100 / 100), +232 u32 grants[2] (running counts of the
  chest and guardian picks the partner got), +240 f32 clock_adjust, +244 u32 adjust_seq (run sync:
  the exe adds clock_adjust to its run clock once per new adjust_seq while playing), +248 f32
  partner_end (run sync: the partner's run clock at its death, 0 unknown), reserved to 256
  outside the seqlock: +256 u32 ev_head (companion), +260 u32 ev_tail (exe), +264 EV_RING events
  {u32 spell, f32 amount, x, y, u32 school, u32 crit | hit kind << 8 | type << 16}, type 0 a hit,
  1 a cast at a unit, 2 at a point, 3 without a target

Full sync (find_sync / hook_hits; without its signatures the ally keeps the dps estimate):
- block "AZSHITS1" (hits_rva) the exe fills for the companion: +8 u32 head, +12 u32 chests bought,
  +16 u32 guardians slain (running counts), +24 ring of 256 events in the same format: every hit
  my hero, summons and procs land (final damage after the game's multiplier) and every SPELL_GO my
  hero casts, with the enemy's interpolated position.
- my damage stats (find_damage_stats / hook_damage_stats: the run's total, per spell, per source and
  the boss fight's per source) skip every hit dealt under REPLAYING, so each player sees only their own.
- the tick replays the partner's events: a hit on the live enemy nearest to its point (else to
  the ally) through the game's enemy damage with the partner's final amount, as a secondary hit
  and never captured as mine; a cast as SPELL_GO from the ally. Events it can't replay are dropped.
- picks: each grant the partner's count is ahead adds one pending chest / guardian pick, which the
  game opens through its own roller; granted picks never move the counters I report, so no echo.
- the ally is my party member (SMSG_GROUP_LIST): stock party frame and minimap dot, fed by its
  health / max health / level values that follow the block.

Boss sync (find_boss_sync / hook_boss_chase, meta boss_sync = 1; needs the full sync, and without its
signatures bosses stay solo). "Bosses" here are the zone's timed bosses (the last one the final boss)
and the lair guardians, told apart by two fields of the enemy record that enemy damage's death branch
reads (boss / guardian, each -1 when the record isn't one).
- Keys pair the two clients' bosses: a guardian is 0x10000 | lair << 8 | life (its lair's n-th
  guardian; lairs come from the zone, not the run's RNG), a timed boss 0x20000 | n (n-th in the run,
  on the shared run clock). Up to BOSS_SLOTS per client.
- the exe publishes in the hits block, +HITS_BOSS: BOSS_SLOTS u64 {u32 key, f32 health fraction},
  key 0 = empty, fraction 0 = dead or gone (kept until the slot is needed again), one store each.
- the companion writes the partner's into the ghost block, outside the seqlock: +3344 u32 on (the
  partner streams boss health), +3348 u32 count, +3352 BOSS_SLOTS u64 in the same format.
- while live (playing, the ally shown and fresh, on): each tick every live boss whose fraction is
  above the partner's for the same key by more than BOSS_EPS (or whose partner boss is dead) takes the
  difference (health - partner fraction * its max health; health + max health to die) through enemy damage
  as a secondary hit, like a replayed one, so the game's own death and rewards run; fractions only
  go down, so the lower one wins on both screens. My hits on bosses aren't streamed (their health
  is), and the partner's replayed hits never land on a boss.
- the enemy AI chases a cached copy of my hero's position; at each record's turn a boss nearer to
  the ally (from where it is heading) gets the ally's position instead, and what its turn queues
  against my hero (melee hits, casts, abilities) is dropped at the next turn: it walks to the ally
  and fights it there, harmless to me.

control bit0 END_RUN_DEFEAT: the partner died, so this run ends in defeat too. On the first tick
that sees it during a run: a centre-screen announcement through the game's own big-message path
(SMSG_MESSAGECHAT type 0x5a, the builder the game uses for its announcements) "Your ally <nick>
has fallen!" / "Союзник <nick> погиб!" by `lang`, and the ally falls dead (values update: health
0, stand state dead) and stays lying there. DEFEAT_DELAY s of run clock later, while playing, the
tick calls the game's own hero-damage function with a lethal hit (state, 1e9, no source, 0) until
the hero is dead: the stock death path (hp 0, phase dead, run end, AZSEND result=defeat, normal
end screen). With run sync and a partner_end, the run clock (and the death time, so a hit that
doesn't kill is retried at once) is set to it right before that hit: both end screens show the
partner's time of death. Latched once per run and reset when a new run starts; the companion
clears the bit for a new run/match.

Run sync (find_run_sync / hook_run_sync; meta ghost_sync = 1, without its signatures co-op keeps
the stock pauses and start). The exe publishes outside the seqlock, after every run tick:
+3336 f32 run clock, +3340 u32 phase (0 ready, 1 playing, 2 choosing, 3 paused, 4 dead, 5 victory).
control bits the companion sets for a co-op match:
- bit1 COOP_LIVE: no pause. The server's call of the run tick goes through a wrapper: in phase
  choosing / paused it runs the tick as `playing` (clock, waves, enemies, hits go on while the pick
  or the menu is open) and puts the real phase back afterwards, unless the tick moved it (death,
  victory). While it does, the roller opens no new pick (pending counters stay and the game rolls
  them after the current pick, through its own chain). Also enables clock_adjust.
- bit2 HOLD_START: the guest's first step no longer starts the run (the run start hidden in the
  movement handler is skipped).
- bit3 START_NOW: the host's run is going: in phase ready the wrapper sets the game's own autostart
  flag, so the run starts on this tick.
"""
import re
import struct
from offsets import PatchError
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coop_shared as shared  # noqa: E402  shared enemies (installed from patch() below)
import coop_choice as choice  # noqa: E402  optional selection protection
import coop_choice_cast as choice_cast  # noqa: E402  owned packet filtering
import coop_choice_push as choice_push  # noqa: E402  space after selecting a reward

ALLY_GUID = 0x0000_0000_7FFF_FF01  # player high guid, far above the hero's
GHOST_GUID = 0xF130_0023_29FF_FFF0  # creature high guid, wisp entry 9001, low id above live wisps
GHOST_ENTRY = 9001
FACTION_FRIENDLY = 35
GHOST_FLAGS = 0x2000002  # NOT_SELECTABLE | NON_ATTACKABLE, as the game's own companion
SPEEDS = (2.5, 7.0, 4.5, 4.7222223, 2.5, 3.1415927)  # walk, run, back, swim, swim back, turn
# player models by race id (1.12): male, female
DISPLAYS = [0, 0, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 1563, 1564, 1478, 1479]
DEFAULT_DISPLAY = 51
MIN_INTERVAL = 0.08  # s of run clock between moves
TIMEOUT = 3.0  # s of run clock without a companion update: despawn
VISIBLE_ITEM = 0x104  # PLAYER_VISIBLE_ITEM_1_0, 12 fields per slot
SLOTS = 19

# block layout
BLOCK_SIZE, PAYLOAD = 3416, (8, 256)
B_SEQ, B_ACTIVE, B_POS, B_RACE, B_CLASS, B_GENDER, B_HINT = 8, 12, 16, 32, 36, 40, 44
B_LOOK, B_FACIAL, B_LEVEL, B_ITEMS, B_NICK, B_CONTROL, B_LANG = 48, 52, 56, 72, 148, 216, 220
B_HP, B_HPM, B_GRANTS, B_EV_HEAD, B_EV_TAIL, B_EV = 224, 228, 232, 256, 260, 264
EV_RING, EV_SIZE = 128, 24  # {u32 spell, f32 amount, x, y, u32 school, crit}
END_RUN_DEFEAT = 1
LETHAL = 100.0  # the shared defeat's hit: twice the hero's health plus this (a 1e9 hit inflated the
# run's and the save's damage-taken stats); repeated each tick until the hero is dead
DEFEAT_DELAY = 2.5  # s of run clock between the announcement and the hero's death
MESSAGES = [(b"Your ally ", b" has fallen!"), ("Союзник ".encode(), " погиб!".encode())]
# run sync: the guest's step while the host hasn't started (no nick), at most every NOTE_EVERY s
WAIT_MESSAGES = [(b"Waiting for the host to start the run", b""), ("Ждём, когда хост начнёт забег".encode(), b"")]
NOTE_EVERY = 4.0
NICK_LEN = 16

# private state: header, then the raw seqlock read (TMP) and the committed snapshot (SNAP)
SPAWNED, LAST_SEQ, LAST_MOVE, LAST_UPD, LAST_POS, LAST_CLOCK, SPLINE, CLOCK_NOW, NEW, DEFEATED = \
    0, 4, 8, 12, 16, 28, 32, 36, 40, 41
LAST_ATTACK, DMG_EACH, N_TARGETS, SPELL_ID, TARGET, SCHOOL = 44, 48, 52, 56, 64, 72
MOVED, SUM_KIND, DUR, SUM_I, PREV_POS, SUM_WANT, SUM_TPL = 42, 76, 80, 84, 88, 100, 104
ANNOUNCED, DEFEAT_AT, ALLY_DEAD = 43, 112, 116
LAST_HP, LAST_HPM, LAST_LVL = 120, 124, 128
# full sync: the run state for the SPELL_GO hook, the replay flags the hit hook reads, picks
STATE_PTR, REPLAYING, OVERRIDE, REPLAY_DMG, GRANTED, SEEN_OPENED, SEEN_SLAIN = 136, 144, 145, 148, 152, 160, 164
EV_N, EV_POS, EV_GUID, EV_ENTRY, APPLIED, SAVED_KIND, CLASS_SPELL = 168, 172, 184, 192, 216, 217, 220
IN_PARTY = 224
FAKED, ADJ_SEEN = 228, 232  # run sync: the real phase while a tick runs as playing, last adjust_seq
HELD, NOTE_AT, REAL_T = 236, 240, 244  # a step the gate held, when the wait note was shown, s since start
DEFEAT_HIT = 237  # only the deliberate shared-defeat call bypasses choice mitigation
SMSG_GROUP_LIST = 0x7D
TMP, SNAP, MSG = 256, 512, 768
# boss sync: guids (contiguous: the replay's exclude slice), keys, damage to deal, guardian lives per lair,
# timed bosses seen (cleared together on a new run); the live verdict; the AI hook's turn state
BOSS_GUIDS, BOSS_KEYS, BOSS_DMG, BOSS_LIVES, BOSS_COUNT, BOSS_RESET = 896, 960, 992, 1024, 1032, 140
BOSS_LIVE, LURED, HERO_XY, HERO_Z, LURED_SLOTS = 1036, 1037, 1040, 1048, 1056  # 4 saved AI stack slots
STATE_SIZE = 1088
BOSS_SLOTS = 8
B_BOSS_ON, B_BOSS_N, B_BOSS = 3344, 3348, 3352
BOSS_EPS = 0.001  # fractions closer than this are left alone (rounding, max health differences)
GUARDIAN_KEY, BOSS_KEY = 0x10000, 0x20000
B_DPS, B_FLAGS, B_SPELL, B_SUMMON_COUNT, B_SUMMONS = 60, 64, 68, 164, 168
B_ADJ, B_ADJ_SEQ, B_END, T_CLOCK, T_PHASE = 240, 244, 248, 3336, 3340
COOP_LIVE, HOLD_START, START_NOW = 2, 4, 8

# mirrored summons: up to 4 client-only creatures that follow the ally at the partner's offsets
SUMMON_GUID = 0xF130_0023_29FF_FFE0  # + index, creature guids next to the ghost's
SUMMONS = 4
# kind -> display: 1 imp, 2 voidwalker, 3 succubus, 4 felhunter, 5 infernal, 6 doomguard,
# 7 felguard, 8 hunter/other pet (dire wolf), 9 companion critter (squirrel)
SUMMON_DISPLAYS = [0, 4449, 1132, 4162, 850, 169, 1912, 5049, 741, 134]
FLAG_DEAD, FLAG_PAUSED, FLAG_EVENTS = 2, 4, 8

# ally attacks: every ATTACK_INTERVAL s of run clock the partner's damage since the last
# attack (dps * elapsed, at most MAX_WINDOW s) is split over up to MAX_TARGETS enemies nearest
# to the ally within RADIUS yd, through the game's own enemy-damage function (damage log, kills,
# loot as usual), with a SMSG_SPELL_GO of the class spell from the ally for the visual
# QUERY_LIMIT: the empty exclude slice's (dangling, aligned) pointer, as Rust passes it
ATTACK_INTERVAL, MAX_WINDOW, RADIUS, MAX_TARGETS, QUERY_LIMIT = 1.0, 2.0, 30.0, 3, 8
MAX_DPS = 1e6  # the companion's dps is clamped to [0, MAX_DPS]
# class id -> (spell id, school): 1.12 ranks 1, the visual the client knows for that spell
CLASS_SPELLS = {1: (78, 0), 2: (879, 1), 3: (3044, 6), 4: (1752, 0), 5: (585, 1), 7: (403, 3),
                8: (133, 2), 9: (686, 5), 11: (5176, 3)}
DEFAULT_SPELL = (133, 2)
LONG_AGO = 0xCE6E6B28  # -1e9 as f32: forces a timeout until a fresh seq arrives


def snap(block_off):
    """Offset in the private state of a block field's committed copy."""
    return SNAP + block_off - PAYLOAD[0]


# run tick entry: movaps xmm6, xmm1 ; mov r13, rcx ; movzx eax, byte [rcx+phase] ; test al, al
TICK = re.compile(rb"\x0f\x28\xf1((?:\x49\x89\xcd|\x4c\x8b\xe9)\x0f\xb6\x81(....))\x84\xc0", re.S)
# the run clock is added to by dt right after: movss xmm7, [r13+clock] ; addss xmm7, xmm6
CLOCK = re.compile(rb"\xf3\x41\x0f\x10\xbd(....)\xf3\x0f\x58\xfe", re.S)
# the spawned companion is pushed into the outbound queue: lea rax, [r13+queue] right before
QUEUE = re.compile(rb"\x4c\x8b\xad(....)\x49\x8d\x85(....)\x48\x89\x85", re.S)
# ... and its push compares len with cap: mov rsi, [rax+queue+0x10] ; cmp rsi, [rax+queue]
QUEUE_PUSH = re.compile(rb"\x48\x8b\xb0(....)\x48\x3b\xb0(....)", re.S)
# __rust_alloc(size, align): the most common target of `mov ecx, 0x20; mov edx, 1; call`
# (the stock packet buffers), also used by the destroy builder (mov word [rsi+0x18], 0xaa)
ALLOC_CALL = re.compile(rb"\xb9\x20\x00\x00\x00\xba\x01\x00\x00\x00\xe8(....)", re.S)
DESTROY_OPCODE = b"\x66\xc7\x46\x18\xaa\x00"
# grow_one for 32-byte, 8-aligned elements: elem size and align passed on the stack
GROW_SIZE = b"\x48\xc7\x44\x24\x28\x20\x00\x00\x00"
GROW_ALIGN = b"\x48\xc7\x44\x24\x20\x08\x00\x00\x00"
# name query reply: name ptr/len and race/gender/class bytes of the hero go to the builder,
# guid of the query in rdx: ... mov [rsp+0x30], r10b ; mov [rsp+0x28], cl ; mov [rsp+0x20], al ;
# lea rbx, [rbp+X] ; mov rcx, rbx ; call <SMSG_NAME_QUERY_RESPONSE builder>
NAME_REPLY = re.compile(rb"\x4c\x8b\x86....\x4c\x8b\x8e....\x0f\xb6\x86....\x0f\xb6\x8e....\x44\x0f\xb6\x96...."
                        rb"\x44\x88\x54\x24\x30\x88\x4c\x24\x28\x88\x44\x24\x20(\x48\x8d\x9d....\x48\x89\xd9)\xe8(....)",
                        re.S)
NAME_OPCODE = re.compile(rb"\x66\xc7\x45.\x51\x00", re.S)  # mov word [rbp+..], 0x51 in the builder
# hero death inside the hero-damage function: mov dword [r13+hp], 0 ; mov byte [r13+phase], 4 ;
# mov dword [r13+ended], 1 ; ... lea r8, ["defeat"...]
DEATH = re.compile(rb"\x41\xc7\x85(....)\x00\x00\x00\x00\x41\xc6\x85(....)\x04\x41\xc7\x85....\x01\x00\x00\x00", re.S)


def packed_guid(g):
    raw = g.to_bytes(8, "little")
    mask = sum(1 << i for i, b in enumerate(raw) if b)
    return bytes([mask]) + bytes(b for b in raw if b)


def f32(v):
    return struct.unpack("<I", struct.pack("<f", v))[0]


def create_packet(ally, guid=None, extra=None):
    """(bytes, offsets) of a one-block SMSG_UPDATE_OBJECT creating the ally player or a creature
    (the ghost, or a mirrored summon with its own guid). extra: more {field index: u32} values;
    offsets["at"](field) is where a field's value sits in the packet."""
    guid = guid or (ALLY_GUID if ally else GHOST_GUID)
    fields = {0: guid & 0xFFFFFFFF, 1: guid >> 32, 2: 0x19 if ally else 9, 4: f32(1.0),
              0x16: 100, 0x1C: 100, 0x22: 1, 0x23: FACTION_FRIENDLY, 0x24: 0,
              0x2E: 0 if ally else GHOST_FLAGS, 0x81: f32(0.389), 0x82: f32(1.5),
              0x83: 0, 0x84: 0, 0x91: f32(1.0)}
    if ally:
        fields.update({0xC1: 0, 0xC2: 0x02000000, 0xC3: 0})
        fields.update({VISIBLE_ITEM + 12 * s: 0 for s in range(SLOTS)})
    else:
        fields[3] = GHOST_ENTRY
    fields.update(extra or {})
    blocks = max(fields) // 32 + 1
    masks = [0] * blocks
    for k in fields:
        masks[k // 32] |= 1 << (k % 32)
    head = struct.pack("<IB", 1, 0) + bytes([3]) + packed_guid(guid) + bytes([4 if ally else 3, 0x70])
    movement = struct.pack("<II3ffI6fI", 0, 0, 0, 0, 0, 0, 0, *SPEEDS, 1)
    values_at = len(head) + len(movement) + 1 + 4 * blocks
    order = sorted(fields)
    pkt = head + movement + bytes([blocks]) + struct.pack(f"<{blocks}I", *masks) \
        + b"".join(struct.pack("<I", fields[k]) for k in order)
    at = lambda field: values_at + 4 * order.index(field) if field in fields else None
    return pkt, {"pos": len(head) + 8, "bytes0": at(0x24), "display": at(0x83), "native": at(0x84),
                 "level": at(0x22), "pbytes": at(0xC1), "pbytes2": at(0xC2), "pbytes3": at(0xC3),
                 "items": at(VISIBLE_ITEM), "at": at}


def move_packet(guid):
    pg = packed_guid(guid)
    pkt = pg + struct.pack("<3fIBIII3f", 0, 0, 0, 0, 0, 0x100, 100, 1, 0, 0, 0)
    n = len(pg)
    return pkt, {"start": n, "spline": n + 12, "duration": n + 21, "dest": n + 29}


def call_target(p, m, group=1):
    return p.offsets.resolve_capture(m.start(group), len(m.group(group)), record=False)


def find_queue(p):
    """Offset of the outbound packet queue (Vec<Packet>) in the run server's state."""
    queues = list(QUEUE.finditer(p.d))
    pushes = {struct.unpack("<i", m.group(2))[0] for m in QUEUE_PUSH.finditer(p.d)
              if struct.unpack("<i", m.group(1))[0] == struct.unpack("<i", m.group(2))[0] + 0x10}
    queue = {struct.unpack("<i", m.group(2))[0] for m in queues} & pushes
    p.check(len(queue) == 1, f"coop ghost: outbound queue offsets {queue}")
    return queue.pop()


def find_alloc(p):
    targets = Counter(call_target(p, m) for m in ALLOC_CALL.finditer(p.d))
    p.check(bool(targets), "coop ghost: no packet buffer allocations found")
    alloc = targets.most_common(1)[0][0]
    at = p.d.find(DESTROY_OPCODE)
    near = [call_target(p, m) for m in ALLOC_CALL.finditer(p.d, at - 0x40, at)]
    p.check(near == [alloc], f"coop ghost: destroy builder allocates with {near}, not {alloc:x}")
    return alloc


def find_grow(p, queue):
    """grow_one of the outbound queue, from the stock push: lea rcx, [r13+queue] ; call."""
    site = re.compile(re.escape(b"\x49\x8d\x8d" + struct.pack("<i", queue) + b"\xe8") + b"(....)", re.S)
    targets = {call_target(p, m) for m in site.finditer(p.d)}
    p.check(len(targets) == 1, f"coop ghost: queue grow targets {targets}")
    grow = targets.pop()
    head = bytes(p.d[p.off(grow):p.off(grow) + 0x40])
    p.check(GROW_SIZE in head and GROW_ALIGN in head, f"coop ghost: {grow:x} is not grow_one<32, 8>")
    return grow


def find_dealloc(p):
    """__rust_dealloc: most common target of `shl rdx, 3 ; mov r8d, 8 ; call` (freeing Vec<u64>)."""
    site = re.compile(rb"\x48\xc1\xe2\x03\x41\xb8\x08\x00\x00\x00\xe8(....)", re.S)
    targets = Counter(call_target(p, m) for m in site.finditer(p.d))
    p.check(bool(targets), "coop: no Vec<u64> frees found")
    return targets.most_common(1)[0][0]


def find_enemy_query(p, clock):
    """Enemies near a point: (out Vec<guid>, state, &pos, radius in xmm3, &[guid] exclude slice as ptr, len)."""
    # push rbp, rsi, rdi ; sub rsp, frame ; lea rbp, [rsp+X] ; mov [rbp+..], -2 ; mov rsi, rcx ;
    # movaps xmm0, [rbp+..] ; movss xmm1, [rdx+clock] ; movss [rbp+..], xmm1 ; mulss xmm3, xmm3
    sig = re.compile(rb"\x55\x56\x57\x48\x81\xec....\x48\x8d\xac\x24....\x48\xc7\x45.\xfe\xff\xff\xff\x48\x89\xce"
                     rb"\x0f\x28\x45.\xf3\x0f\x10\x8a" + re.escape(struct.pack("<i", clock))
                     + rb"\xf3\x0f\x11\x4d.\xf3\x0f\x59\xdb", re.S)
    hits = [p.va(m.start()) for m in sig.finditer(p.d)]
    p.check(len(hits) == 1, f"coop: enemy query {len(hits)} hits")
    return hits[0]


def find_enemy_damage(p, phase):
    """Enemy damage: (state, guid, f32 amount in xmm2, spell id, school, crit, flag); it keeps the
    spell id at once and bails out unless the run phase is `playing`."""
    sig = re.compile(rb"\x44\x89\x8d....\x80\xb9" + re.escape(struct.pack("<i", phase)) + rb"\x01\x0f\x85", re.S)
    starts = {func_start(p, p.va(m.start())) for m in sig.finditer(p.d)}
    starts.discard(None)
    p.check(len(starts) == 1, f"coop: enemy damage function {starts}")
    return starts.pop()


# enemy damage's prologue: 8 pushes ; sub rsp, frame ; lea rbp, [rsp+X] (stack args sit above it)
FRAME = re.compile(rb"\x55\x41\x57\x41\x56\x41\x55\x41\x54\x56\x57\x53\x48\x81\xec(....)\x48\x8d\xac\x24(....)", re.S)
# its damage-log call: mov r8, [rdi+hero guid] ; ... movzx ecx, byte [rbp+crit] ; mov [rsp+0x30], cl ;
# movzx ecx, byte [rbp+school] ; mov [rsp+0x28], cl ; mov [rsp+0x20], eax ; lea rcx, [rbp+..] ;
# mov rdx, rsi ; call ; mov rsi, [rdi+queue len]
DAMAGE_LOG = re.compile(rb"\x4c\x8b\x87(....).{8,96}?\x0f\xb6\x8d(....)\x88\x4c\x24\x30\x0f\xb6\x8d(....)\x88\x4c\x24\x28"
                        rb"\x89\x44\x24\x20\x48\x8d\x8d....\x48\x89\xf2\xe8....\x48\x8b\xb7(....)", re.S)


def find_damage_slots(p, enemy_damage, phase, queue):
    """Where enemy damage keeps its spell id, school and crit (rbp slots the hit hook reads) and
    the state's hero guid, from its own code; the school and crit slots must be its 5th and 6th
    stack args by the frame size, and the damage log must push to the queue found elsewhere."""
    lo, hi = (p.off(v) for v in func_range(p, enemy_damage))
    frame = FRAME.match(p.d, lo)
    spell = re.compile(rb"\x44\x89\x8d(....)\x80\xb9" + re.escape(struct.pack("<i", phase)), re.S).search(p.d, lo, lo + 0x100)
    logs = list(DAMAGE_LOG.finditer(p.d, lo, hi))
    p.check(bool(frame and spell and logs), "coop: enemy damage slots not found")
    arg = lambda n: 8 * n + 0x40 + s32(frame.group(1)) - s32(frame.group(2))  # rbp offset of stack arg n
    slots = {(s32(m.group(1)), s32(m.group(3)), s32(m.group(2)), s32(m.group(4))) for m in logs}
    p.check(len(slots) == 1, f"coop: enemy damage log calls disagree {slots}")
    hero_guid, school, crit, queue_len = slots.pop()
    p.check(school == arg(5) and crit == arg(6) and queue_len == queue + 0x10
            and b"\x8b\x85" + spell.group(1) in p.d[lo:hi],  # mov eax, [rbp+spell] later on
            "coop: enemy damage slots disagree")
    return {"spell": s32(spell.group(1)), "school": school, "crit": crit, "hero_guid": hero_guid}


def find_announce(p):
    """The game's big-announcement builder: SMSG_MESSAGECHAT type 0x5a (out, name, name len, text,
    text len): mov byte [rax], 0x5a ; mov dword [rax+1], 0, in a function that keeps its args
    in rsi, r14, rbx, rdi."""
    sig = re.compile(re.escape(b"\xc6\x00\x5a\xc7\x40\x01\x00\x00\x00\x00"))
    starts = {func_start(p, p.va(m.start())) for m in sig.finditer(p.d)}
    starts.discard(None)
    # args kept as rsi out, r14 name, rbx name len, rdi text (the text len is its 5th, stack arg)
    args = b"\x4c\x89\xcf\x4c\x89\xc3\x49\x89\xd6\x48\x89\xce"
    starts = {s for s in starts if args in p.d[p.off(s):p.off(s) + 0x40]}
    p.check(len(starts) == 1, f"coop: announcement builder {starts}")
    return starts.pop()


def values_packet(fields):
    """(bytes, {field: offset of its value}) of an SMSG_UPDATE_OBJECT values update for the ally."""
    blocks = max(fields) // 32 + 1
    masks = [0] * blocks
    for k in fields:
        masks[k // 32] |= 1 << (k % 32)
    head = struct.pack("<IB", 1, 0) + b"\x00" + packed_guid(ALLY_GUID) + bytes([blocks]) \
        + struct.pack(f"<{blocks}I", *masks)
    order = sorted(fields)
    return (head + b"".join(struct.pack("<I", fields[k]) for k in order),
            {k: len(head) + 4 * i for i, k in enumerate(order)})


def death_packet():
    """Values update for the ally: health 0 and stand state dead (7)."""
    return values_packet({0x16: 0, 0x8A: 7})[0]


def find_name_replies(p):
    """Call sites of the SMSG_NAME_QUERY_RESPONSE builder with the hero's name; [] = fallback."""
    out = []
    for m in NAME_REPLY.finditer(p.d):
        target = p.off(call_target(p, m, 2))
        if NAME_OPCODE.search(p.d, target, target + 0x80):
            out.append(m)
    return out


def func_start(p, va):
    """Start of the function containing va, from the exception table (.pdata)."""
    found = func_range(p, va)
    return found and found[0]


def func_range(p, va):
    """(start, end) vas of the function containing va, from the exception table (.pdata)."""
    # Preserve the existing optional-finder contract, while sharing the validated
    # exception-directory index with the rest of address recovery.
    try:
        return p.offsets.function_range(va)
    except PatchError:
        return None


def find_hero_damage(p, phase):
    """The game's hero-damage function (state, f32 amount, source, flag): it holds the death
    branch that reports `defeat`, and starts by checking the run phase. Returns (function, offset
    of the hero's health, the f32 that branch zeroes)."""
    found = set()
    evidence = []
    defeat = p.d.find(b"defeatleftvictory")
    for m in DEATH.finditer(p.d):
        if struct.unpack("<i", m.group(2))[0] != phase:
            continue
        near = p.d[m.end():m.end() + 0x40]
        lea = re.search(rb"\x4c\x8d\x05(....)", near, re.S)
        if not lea or p.va(m.end() + lea.end()) + struct.unpack("<i", lea.group(1))[0] != p.va(defeat):
            continue
        start = func_start(p, p.va(m.start()))
        if start and struct.pack("<i", phase) in p.d[p.off(start):p.off(start) + 0x70]:
            found.add((start, s32(m.group(1))))
            evidence.append(m)
    p.check(len(found) == 1, f"coop: hero damage function {found}")
    p.offsets.field('RunState', 'phase', [p.va(m.start(2) - 3) for m in evidence], base='r13', width=1)
    p.offsets.field('RunState', 'hero_hp', [p.va(m.start()) for m in evidence], base='r13', width=4)
    return found.pop()


# --- full sync: the hero's real hits and casts, shared reward picks -------------------------------
# Struct offsets and frame slots are read out of the matched game code (they move between builds:
# 107 grew the enemy record from 0x160 to 0x1d8 bytes) and cross-checked against the game's own
# uses, so a layout change fails the build instead of reading the wrong fields.
# Inside enemy damage, after the target lookup and the damage multipliers (107 added a second one,
# mulss xmm8, xmm0), before hp drops: movss xmm3, [r14+r13+hp] ; movss xmm2, [r14+r13+move duration]
# (record = r14+r13, r14+r12 after 109: SIB 0x2e / 0x26; state = rdi, final damage = xmm8)
HIT_SITE = re.compile(rb"\xf3\x44\x0f\x59\xc6(?:\xf3\x44\x0f\x59\xc0)?\xf3\x44\x0f\x5f\x05...."
                      rb"(\xf3\x43\x0f\x10\x9c[\x2e\x26]....\xf3\x43\x0f\x10\x94[\x2e\x26](....))", re.S)
INDEX = {0x2E: "r13", 0x26: "r12"}  # SIB byte of [r14 + index] -> the record index register
# right before it the scan for the target record: add r13, record size ; cmp rax, r13 ; je ;
# cmp [r14+r13+guid], rsi ; jne (r12 after 109)
ENEMY_SCAN = re.compile(rb"\x49\x81[\xc5\xc4](....)\x4c\x39[\xe8\xe0]\x0f\x84....\x4b\x39\xb4[\x2e\x26](....)\x75.",
                        re.S)
# right after it the position lerp: movss xmm3, [rdi+clock] ; subss xmm3, [r14+r13+move start] ;
# divss ; maxss ; movss xmm2, [1.0] ; minss ; movsd xmm1, [r14+r13+from] ; movsd xmm3, [r14+r13+to]
ENEMY_LERP = re.compile(rb"\xf3\x0f\x10\x9f(....)\xf3\x43\x0f\x5c\x9c[\x2e\x26](....)\xf3\x0f\x5e\xda\xf3\x0f\x5f\xcb"
                        rb"\xf3\x0f\x10\x15....\xf3\x0f\x5d\xd1\xf2\x43\x0f\x10\x8c[\x2e\x26](....)"
                        rb"\xf2\x43\x0f\x10\x9c[\x2e\x26](....)", re.S)
# at its entry the records Vec: mov r14, [rcx+ptr] ; mov rcx, [rcx+len] ; imul rax, rcx, record size
ENEMY_VEC = re.compile(rb"\x4c\x8b\xb1(....)\x48\x8b\x89(....)\x48\x69\xc1(....)", re.S)
# a secondary hit saves and sets the state's hit-kind byte (0 hero, 1 dot tick, 2 summon,
# 3 secondary: no procs, no spread): mov bl, [rdi+kind] ; mov byte [rdi+kind], 3
HIT_KIND_SET = re.compile(rb"\x8a\x9f(....)\xc6\x87(....)\x03", re.S)
# SMSG_SPELL_GO builder (out, caster guid, spell, hit guids, hit count, &targets, &ammo): its entry
GO_BUILDER = re.compile(rb"\x55\x41\x57\x41\x56\x41\x55\x41\x54\x56\x57\x53\x48\x81\xec...."
                        rb"\x48\x8d\xac\x24\x80\x00\x00\x00\x48\xc7\x45.\xfe\xff\xff\xff\x4c\x89\xcf"
                        rb"\x44\x89\xc3\x49\x89\xd6\x48\x89\xce\x48\x8b\x85....\x44\x8b\x20", re.S)
GO_OPCODE = re.compile(rb"\x66\xc7\x45.\x32\x01", re.S)  # mov word [rbp+..], 0x132 (SMSG_SPELL_GO)
# a chest's pick: cmp byte [rcx+phase], 1 ; jne ; cmp dword [rcx+pending chests], 0 ; je ;
# cmp byte [rcx+no-offers mode], 0 ; jne ; mov dl, 1 ; mov r8b, 1 ; add rsp, .. ; pops ; jmp roller
CHEST_ROLL = re.compile(rb"\x80\xb9(....)\x01\x75.\x83\xb9(....)\x00\x74.\x80\xb9(....)\x00\x75."
                        rb"\xb2\x01\x41\xb0\x01\x48\x83\xc4.(?:\x41?[\x58-\x5f]){1,8}\xe9(....)", re.S)
# a guardian's pick: cmp dword [r13+pending guardians], 0 ; je ; cmp byte [r13+mode], 0 ; jne ;
# cmp byte [r13+phase], 1 ; jne ; mov rcx, r13 ; mov dl, 2 ; mov r8b, 1 ; call roller
GUARD_ROLL = re.compile(rb"\x41\x83\xbd(....)\x00\x74.\x41\x80\xbd(....)\x00\x75.\x41\x80\xbd(....)\x01\x75."
                        rb"\x4c\x89\xe9\xb2\x02\x41\xb0\x01\xe8(....)", re.S)
# caches bought: inc dword [rsi+opened] right before inc dword [rsi+pending chests]
OPENED = rb"\xff\x86(....)\xff\x86"
# guardians slain: mov dword [rdi], 0 ; inc dword [rdi+slain] ; mov byte [rbp+..], 0
SLAIN = re.compile(rb"\xc7\x07\x00\x00\x00\x00\xff\x87(....)\xc6\x85", re.S)
HITS_RING, HIT_SIZE = 256, 24
HITS_BOSS = 24 + HITS_RING * HIT_SIZE  # my bosses' keys and health fractions (boss sync)
EV_PER_TICK = 16  # partner events replayed per run tick at most
MAP_RADIUS = 8.0  # yd around the partner's target point to find a local enemy
HIT, CAST_UNIT, CAST_POINT, CAST_SELF = 0, 1, 2, 3


def s32(b):
    return struct.unpack("<i", b)[0]


def one(p, pattern, what):
    hits = list(pattern.finditer(p.d))
    p.check(len(hits) == 1, f"coop sync: {what} {len(hits)} hits")
    return hits[0]


def find_roller(p, phase):
    """The chest and guardian pick sites and the pick roller they call."""
    chest = one(p, CHEST_ROLL, "chest pick")
    guard = one(p, GUARD_ROLL, "guardian pick")
    roller = call_target(p, chest, 4)
    p.check(s32(chest.group(1)) == phase == s32(guard.group(3)) and s32(chest.group(3)) == s32(guard.group(2))
            and call_target(p, guard, 4) == roller, "coop sync: chest and guardian picks disagree")
    return chest, guard, roller


# right after the tick entry: jne ; cmp byte [r13+autostart], 0 ; je ; mov byte [r13+autostart], 0 ;
# mov rcx, r13 ; call start_run (phase ready -> playing)
AUTOSTART = re.compile(rb"\x75.\x41\x80\xbd(....)\x00\x74.\x41\xc6\x85(....)\x00\x4c\x89\xe9\xe8(....)", re.S)
# the movement handler starts the run on the first step: cmp byte [reg+phase], 0 ; jne skip ; ... ;
# call start_run, where skip is right behind that call
MOVE_START = rb"\x80[\xb8-\xbf]%s\x00\x75(.)"
# the server calls the run tick: movaps xmm1, xmmN (dt) ; call tick
TICK_CALL = re.compile(rb"\x0f\x28[\xc8-\xcf]\xe8(....)", re.S)


def find_run_sync(p, tick, phase):
    """The run start (the game's autostart flag, the start function and its call in the movement
    handler), the server's call of the run tick and the pick roller (with its prologue)."""
    auto = AUTOSTART.match(p.d, tick.end())
    p.check(auto is not None and auto.group(1) == auto.group(2), "run sync: autostart not after the tick entry")
    start = call_target(p, auto, 3)
    moves = []
    for m in re.finditer(MOVE_START % re.escape(struct.pack("<i", phase)), p.d, re.S):
        skip = m.end() + m.group(1)[0]
        call = p.d[skip - 5:skip]
        if call[0] == 0xE8 and p.va(skip) + s32(call[1:]) == start:
            moves.append(p.va(skip - 5))
    p.check(len(moves) == 1, f"run sync: {len(moves)} run starts in the movement handler")
    tick_fn = func_start(p, p.va(tick.start()))
    calls = [p.va(m.start() + 3) for m in TICK_CALL.finditer(p.d) if call_target(p, m) == tick_fn]
    p.check(len(calls) == 1, f"run sync: {len(calls)} calls of the run tick")
    roller = find_roller(p, phase)[2]
    prologue = b"\x55\x41\x57\x41\x56"  # push rbp ; push r15 ; push r14
    p.check(p.d[p.off(roller):p.off(roller) + 5] == prologue, "run sync: roller prologue changed")
    return {"autostart": s32(auto.group(1)), "start": start, "move_call": moves[0], "tick_call": calls[0],
            "tick": tick_fn, "roller": roller, "prologue": prologue}


def find_sync(p, phase, clock, enemy_damage):
    """Everything the full sync hooks or calls, with the enemy record layout the caves read;
    raises PatchError when the game changed."""
    hit = one(p, HIT_SITE, "hit capture site")
    lo, hi = func_range(p, p.va(hit.start())) or (None, None)
    p.check(lo == enemy_damage, "coop sync: hit site outside enemy damage")
    lo, hi = p.off(lo), p.off(hi)
    vec = ENEMY_VEC.search(p.d, lo, lo + 0x100)
    scan = ENEMY_SCAN.search(p.d, hit.start() - 0x40, hit.start())
    move = ENEMY_LERP.search(p.d, hit.end(), hit.end() + 0x80)
    p.check(bool(vec and scan and move), "coop sync: enemy record code not found")
    enemies, size, e_from = s32(vec.group(1)), s32(scan.group(1)), s32(move.group(3))
    sib = hit.group(1)[5]
    # one index register everywhere: the SIBs, add/cmp of the scan (r13: c5/e8, r12: c4/e0)
    sibs = {hit.group(1)[5], hit.group(1)[15], p.d[scan.end() - 7], p.d[move.start(2) - 1],
            p.d[move.start(3) - 1], p.d[move.start(4) - 1]}
    p.check(sibs == {sib} and p.d[scan.start() + 2] == 0xC5 - (sib == 0x26)
            and p.d[scan.start() + 9] == 0xE8 - 8 * (sib == 0x26), "coop sync: enemy record index register disagrees")
    z_from = b"\xf3\x43\x0f\x10\x8c" + bytes([sib]) + struct.pack("<i", e_from + 8)  # movss xmm1, [r14+idx+from z]
    p.check(s32(vec.group(2)) == enemies + 8 and s32(vec.group(3)) == size and s32(move.group(1)) == clock
            and s32(move.group(4)) == e_from + 12 and z_from in p.d[move.end():move.end() + 0x30],
            "coop sync: enemy record layout disagrees")
    kinds = {s32(m.group(1)) for m in HIT_KIND_SET.finditer(p.d, lo, hi) if m.group(1) == m.group(2)}
    p.check(len(kinds) == 1, f"coop sync: hit kind byte {kinds}")
    kind = kinds.pop()
    p.check(b"\x88\x9f" + struct.pack("<i", kind) in p.d[lo:hi], "coop sync: hit kind is never restored")
    layout = {"enemies": enemies, "size": size, "guid": s32(scan.group(2)), "from": e_from, "to": e_from + 12,
              "start": s32(move.group(2)), "dur": s32(hit.group(2)), "kind": kind, "hp": s32(hit.group(1)[6:10]),
              "idx": INDEX[sib]}
    go = one(p, GO_BUILDER, "SMSG_SPELL_GO builder")
    p.check(GO_OPCODE.search(p.d, go.end(), go.end() + 0x40) is not None, "coop sync: not the SPELL_GO builder")
    chest, guard, roller = find_roller(p, phase)
    opened = one(p, re.compile(OPENED + re.escape(chest.group(2)), re.S), "caches opened")
    slain = one(p, SLAIN, "guardians slain")
    p.offsets.field('RunState', 'clock', [p.va(move.start())], base='rdi', width=4)
    p.offsets.field('RunState', 'enemies', [p.va(vec.start())], base='rcx', width=8)
    p.offsets.field('RunState', 'enemies_len', [p.va(vec.start(2) - 3)], base='rcx', width=8)
    for name, at, width in [('hp', hit.start(1), 4), ('dur', hit.start(1) + 10, 4),
                            ('guid', scan.start(2) - 4, 8), ('start', move.start(2) - 6, 4),
                            ('from', move.start(3) - 6, 8), ('to', move.start(4) - 6, 8)]:
        p.check(p.offsets.field('EnemyRecord', name, [p.va(at)], base='r14', index=INDEX[sib], width=width)
                == layout[name], f'coop: decoded enemy {name} disagrees')
    for name, va in [('run.hit_site', p.va(hit.start(1))), ('packet.spell_go', p.va(go.start())),
                     ('run.roller', roller)]:
        p.offsets.symbol(name, va, evidence='full sync signatures and record/packet cross-checks')
    return {"hit": p.va(hit.start(1)), "hit_bytes": hit.group(1), "go": p.va(go.start()), "roller": roller,
            "chests": s32(chest.group(2)), "guards": s32(guard.group(1)), "mode": s32(chest.group(3)),
            "opened": s32(opened.group(1)), "slain": s32(slain.group(1)), **layout}


# Right after hp drops, enemy damage adds the dealt amount (capped at the health left) to my run's damage
# stats. The run total: cvtss2sd xmm7, xmm1 ; movsd xmm0, [rdi+total] ; addsd xmm0, xmm7 ;
# movsd [rdi+total], xmm0, then the per-spell map: lea rax, [rdi+map] ; mov rcx, [rdi+map]
DMG_TOTAL = re.compile(rb"\xf3\x0f\x5a\xf9\xf2\x0f\x10\x87(....)\xf2\x0f\x58\xc7\xf2\x0f\x11\x87\1"
                       rb"\x48\x8d\x87(....)\x48\x8b\x8f\2", re.S)
# the map entry's add ends it: call entry ; movsd xmm0, [rax] ; addsd xmm0, xmm7 ; movsd [rax], xmm0
DMG_SPELL = re.compile(rb"\xe8....\xf2\x0f\x10\x00\xf2\x0f\x58\xc7\xf2\x0f\x11\x00", re.S)
# the per-source breakdown starts with the source's label: lea rcx, [rbp+label] ; mov rdx, rdi ;
# mov r8d, r14d ; call; then a boss's own breakdown: cmp r15, -1 ; je past it, where the damage log
# starts: test r14d, r14d ; sete al ; or al, bl. After 109 the spell id is loaded on the way
# (mov ebx, [rbp+spell] ; mov r8d, ebx) and the log tests ebx: a skip must still load it (group 1).
DMG_SOURCE = re.compile(rb"\x48\x8d\x8d....\x48\x89\xfa(?:(\x8b\x9d....)\x41\x89\xd8|\x45\x89\xf0)\xe8", re.S)
DMG_NO_BOSS = re.compile(rb"\x49\x83\xff\xff\x0f\x84(....)", re.S)
DMG_LOG = b"\x45\x85\xf6\x0f\x94\xc0\x08\xd8"
DMG_LOG_EBX = b"\x85\xdb\x0f\x94\xc0\x0a\x85"  # test ebx, ebx ; sete al ; or al, [rbp+..]


def find_damage_stats(p, enemy_damage):
    """The two stretches of enemy damage that add a hit to my damage stats: (site, its bytes, where
    it ends) for the run total + per-spell map, and for the per-source breakdowns (the run's, then the
    boss fight's, which resets on the fight's first hit)."""
    lo, hi = (p.off(v) for v in func_range(p, enemy_damage))
    totals = list(DMG_TOTAL.finditer(p.d, lo, hi))
    p.check(len(totals) == 1, f"coop: damage total {len(totals)} hits")
    total = totals[0]
    spell = DMG_SPELL.search(p.d, total.end(), total.end() + 0x180)
    source = spell and DMG_SOURCE.search(p.d, spell.end(), spell.end() + 0x300)  # 109+ logs kills in between
    no_boss = source and DMG_NO_BOSS.search(p.d, source.end(), source.end() + 0x100)
    p.check(bool(no_boss), "coop: damage stats code not found")
    past = no_boss.end() + s32(no_boss.group(1))
    keep = source.group(1) or b""
    log = DMG_LOG_EBX if keep else DMG_LOG
    p.check(no_boss.end() < past < hi and p.d[past:past + len(log)] == log,
            "coop: damage stats don't end at the damage log")
    return {"total": s32(total.group(1)), "map": s32(total.group(2)),
            "sites": [(p.va(total.start()), bytes(p.d[total.start():total.start() + 24]), p.va(spell.end()), b""),
                      (p.va(source.start()), bytes(p.d[source.start():source.end() - 1]), p.va(past), keep)]}


def hook_damage_stats(p, stats, state):
    """Hits dealt under REPLAYING (the partner's replayed hits, boss health sync, the ally's dps
    estimate) skip my damage stats; my own hits, bosses included, still count."""
    for site, code, skip, keep in stats["sites"]:
        def build(va, site=site, code=code, skip=skip, keep=keep):
            cmp = p.asm(f"cmp byte ptr [rip + {state + REPLAYING - (va + 7):#x}], 0", va)
            if keep:  # a replay skips the stats but still runs keep (rbp-relative: loads what the log reads)
                replay = keep + p.asm(f"jmp {skip:#x}", va + len(cmp) + 6 + len(keep))
                head = cmp + b"\x0f\x84" + struct.pack("<i", len(replay)) + replay  # je over it: not replaying
            else:
                head = cmp + p.asm(f"jne {skip:#x}", va + len(cmp))
            return head + code + p.asm(f"jmp {site + len(code):#x}", va + len(head) + len(code))

        cave = p.add_cave(build)
        p.write(site, p.asm(f"jmp {cave:#x}", site).ljust(len(code), b"\x90"), expect=code)
    p.log.append(f"coop: replays kept out of my damage stats (total {stats['total']:#x}, per spell "
                 f"{stats['map']:#x}), at {', '.join(f'{s[0]:x}' for s in stats['sites'])}")


def lerp(tag, state, clock, e):
    """xmm1, xmm2 = x, y of the enemy record in rax now (its move from e["from"] to e["to"] on
    the run clock of the state register; e: the layout find_sync read); uses ecx, xmm0."""
    return f"""
        movss xmm0, dword ptr [{state} + {clock:#x}]
        subss xmm0, dword ptr [rax + {e["start"]:#x}]
        movss xmm1, dword ptr [rax + {e["dur"]:#x}]
        xorps xmm2, xmm2
        ucomiss xmm1, xmm2
        jbe {tag}_at_end
        divss xmm0, xmm1
        maxss xmm0, xmm2
        mov ecx, 0x3f800000
        movd xmm1, ecx
        minss xmm0, xmm1
        jmp {tag}_lerp
    {tag}_at_end:
        mov ecx, 0x3f800000
        movd xmm0, ecx
    {tag}_lerp:
        movss xmm1, dword ptr [rax + {e["to"]:#x}]
        subss xmm1, dword ptr [rax + {e["from"]:#x}]
        mulss xmm1, xmm0
        addss xmm1, dword ptr [rax + {e["from"]:#x}]
        movss xmm2, dword ptr [rax + {e["to"] + 4:#x}]
        subss xmm2, dword ptr [rax + {e["from"] + 4:#x}]
        mulss xmm2, xmm0
        addss xmm2, dword ptr [rax + {e["from"] + 4:#x}]
    """


def hook_hits(p, full, slots, state, hits, clock, boss=None, sh=None, choice_g=None):
    """Two hooks filling the hits block ring for the companion: every hit my hero, its summons and
    procs land (after the game's damage multiplier: the final amount, crit, school, hit kind, the
    enemy's position) and every SMSG_SPELL_GO my hero casts (spell, point or target position).
    While the tick replays a partner hit (REPLAYING) nothing is recorded, and the hit hook swaps the
    multiplied amount for the partner's final one (OVERRIDE, once). While the boss sync is live, hits
    on bosses aren't recorded: their health is shared instead; in shared-enemies mode (sh: the shared
    layout) neither are hits on shared enemies."""
    mine_only = "" if not boss else f"""
            cmp byte ptr [r11 + {BOSS_LIVE}], 0
            je hc_mine
            lea rax, [r14 + {full["idx"]}]
            {is_boss('rax', boss, 'hc_mine')}
            jmp original
        hc_mine:
    """
    if sh:
        mine_only += f"""
            test byte ptr [r11 + {sh['control'] - state:#x}], {shared.LIVE | shared.GUEST}
            jz hc_own
            lea rax, [r14 + {full["idx"]}]
            {shared.is_shared('rax', sh, f"r11 + {sh['sh'] - state:#x}", 'hc_own')}
            jmp original
        hc_own:
    """
    slot = """
        mov ecx, dword ptr [r10 + 8]
        and ecx, {ring}
        imul ecx, ecx, {size}
        lea rdx, [r10 + rcx + 24]
    """.format(ring=HITS_RING - 1, size=HIT_SIZE)
    site, back = full["hit"], full["hit"] + len(full["hit_bytes"])

    def build_hit(va):
        body = p.asm(f"""
            lea r11, [rip + {state - (va + 7):#x}]
            cmp byte ptr [r11 + {REPLAYING}], 0
            je capture
            cmp byte ptr [r11 + {OVERRIDE}], 0
            je original
            mov byte ptr [r11 + {OVERRIDE}], 0
            movss xmm8, dword ptr [r11 + {REPLAY_DMG}]
            jmp original
        capture:
            {choice.outgoing(choice_g, state) if choice_g else ''}
            {mine_only}
            lea r10, [r11 + {hits - state:#x}]
            {slot}
            mov ecx, dword ptr [rbp + {slots['spell']:#x}]
            mov dword ptr [rdx], ecx
            movss dword ptr [rdx + 4], xmm8
            lea rax, [r14 + {full["idx"]}]
            {lerp("hc", "rdi", clock, full)}
            movss dword ptr [rdx + 8], xmm1
            movss dword ptr [rdx + 12], xmm2
            movzx ecx, byte ptr [rbp + {slots['school']:#x}]
            mov dword ptr [rdx + 16], ecx
            movzx ecx, byte ptr [rbp + {slots['crit']:#x}]
            and ecx, 1
            movzx eax, byte ptr [rdi + {full['kind']:#x}]
            shl eax, 8
            or ecx, eax
            mov dword ptr [rdx + 20], ecx
            inc dword ptr [r10 + 8]
        original:
        """, va)
        return body + full["hit_bytes"] + p.asm(f"jmp {back:#x}", va + len(body) + len(full["hit_bytes"]))

    cave = p.add_cave(build_hit)
    p.write(site, p.asm(f"jmp {cave:#x}", site).ljust(len(full["hit_bytes"]), b"\x90"), expect=full["hit_bytes"])

    go = full["go"]
    prologue = bytes.fromhex("554157415641554154565753")  # push rbp, r15, r14, r13, r12, rsi, rdi, rbx

    # at the builder's entry: rcx out, rdx caster, r8d spell, r9 hits, [rsp+0x30] &targets
    # {u32 tag: 0 unit (guid at +8), 1 point (x, y, z at +4), 2 none}; args stay untouched
    def build_go(va):
        return p.asm(f"""
            lea r11, [rip + {state - (va + 7):#x}]
            mov r10, qword ptr [r11 + {STATE_PTR}]
            test r10, r10
            jz go_original
            cmp byte ptr [r11 + {REPLAYING}], 0
            jne go_original
            cmp rdx, qword ptr [r10 + {slots['hero_guid']:#x}]
            jne go_original
            {f'''
            cmp dword ptr [r11 + {choice_g['choice'] + choice.ENABLED - state:#x}], 0
            je go_capture
            test byte ptr [r11 + {choice_g['block'] + B_CONTROL - state:#x}], {COOP_LIVE}
            jz go_capture
            cmp byte ptr [r11 + {FAKED}], 2
            je go_original
            cmp byte ptr [r10 + {choice_g['phase']:#x}], 2
            je go_original
        go_capture:
            ''' if choice_g else ''}
            push rcx
            push rdx
            push rsi
            push rdi
            mov rsi, r10
            lea r10, [r11 + {hits - state:#x}]
            {slot}
            mov rdi, rdx
            mov dword ptr [rdi], r8d
            mov dword ptr [rdi + 4], 0
            mov qword ptr [rdi + 8], 0
            mov dword ptr [rdi + 16], 0
            mov dword ptr [rdi + 20], {CAST_SELF << 16:#x}
            mov rdx, qword ptr [rsp + 0x50]
            test rdx, rdx
            jz go_push
            mov eax, dword ptr [rdx]
            cmp eax, 1
            jne go_unit
            mov rax, qword ptr [rdx + 4]
            mov qword ptr [rdi + 8], rax
            mov dword ptr [rdi + 20], {CAST_POINT << 16:#x}
            jmp go_push
        go_unit:
            test eax, eax
            jnz go_push
            mov rdx, qword ptr [rdx + 8]
            mov rax, qword ptr [rsi + {full['enemies']:#x}]
            mov rcx, qword ptr [rsi + {full['enemies'] + 8:#x}]
        go_find:
            test rcx, rcx
            jz go_push
            cmp qword ptr [rax + {full['guid']:#x}], rdx
            je go_found
            add rax, {full['size']:#x}
            dec rcx
            jmp go_find
        go_found:
            {lerp("gc", "rsi", clock, full)}
            movss dword ptr [rdi + 8], xmm1
            movss dword ptr [rdi + 12], xmm2
            mov dword ptr [rdi + 20], {CAST_UNIT << 16:#x}
        go_push:
            inc dword ptr [r10 + 8]
            pop rdi
            pop rsi
            pop rdx
            pop rcx
        go_original:
            push rbp
            push r15
            push r14
            push r13
            push r12
            push rsi
            push rdi
            push rbx
            jmp {go + len(prologue):#x}
        """, va)

    cave = p.add_cave(build_go)
    p.write(go, p.asm(f"jmp {cave:#x}", go).ljust(len(prologue), b"\x90"), expect=prologue)
    p.log.append(f"coop: full sync, hits {full['hit']:x}, casts {go:x}, roller {full['roller']:x}")


def hook_run_sync(p, rs, block, state, phase, clock, choice_g=None):
    """No pause, the guest's start gate and the host's start for a co-op match (control bits)."""
    control = block + B_CONTROL

    # the server's run tick call: rcx = run state, xmm1 = dt; rdx, r8, r9 pass through untouched
    def build_wrapper(va):
        return p.asm(f"""
            lea rax, [rip + {block - (va + 7):#x}]
            push rbx
            push rsi
            sub rsp, 0x28
            mov rbx, rax
            mov rsi, rcx
            movss xmm0, dword ptr [rbx + {state - block + REAL_T:#x}]
            addss xmm0, xmm1
            movss dword ptr [rbx + {state - block + REAL_T:#x}], xmm0
            test byte ptr [rbx + {B_CONTROL}], {COOP_LIVE}
            jz run
            movzx eax, byte ptr [rsi + {phase:#x}]
            test eax, eax
            jnz not_ready
            test byte ptr [rbx + {B_CONTROL}], {START_NOW}
            jz run
            mov byte ptr [rsi + {rs['autostart']:#x}], 1
            jmp run
        not_ready:
            cmp eax, 2
            jb run
            cmp eax, 3
            ja run
            mov byte ptr [rbx + {state - block + FAKED:#x}], al
            mov byte ptr [rsi + {phase:#x}], 1
        run:
            {f'mov rcx, rsi; call {choice_g["update"]:#x}' if choice_g else ''}
            mov rcx, rsi
            call {rs['tick']:#x}
            {f'mov rcx, rsi; call {choice_g["cast_filter"]:#x}' if choice_g else ''}
            movzx ecx, byte ptr [rbx + {state - block + FAKED:#x}]
            test ecx, ecx
            jz publish
            mov byte ptr [rbx + {state - block + FAKED:#x}], 0
            cmp byte ptr [rsi + {phase:#x}], 1
            jne publish
            mov byte ptr [rsi + {phase:#x}], cl
        publish:
            {f'mov rcx, rsi; call {choice_g["update"]:#x}' if choice_g else ''}
            {f'mov rcx, rsi; call {choice_g["push"]:#x}' if choice_g and choice_g.get('push') else ''}
            mov ecx, dword ptr [rsi + {clock:#x}]
            mov dword ptr [rbx + {T_CLOCK}], ecx
            movzx ecx, byte ptr [rsi + {phase:#x}]
            mov dword ptr [rbx + {T_PHASE}], ecx
            add rsp, 0x28
            pop rsi
            pop rbx
            ret
        """, va)

    wrapper = p.add_cave(build_wrapper)
    site = rs["tick_call"]
    p.write(site, p.asm(f"call {wrapper:#x}", site), expect=p.asm(f"call {rs['tick']:#x}", site))

    # the movement handler's run start: skipped while the guest is held and the host hasn't started
    def build_gate(va):
        return p.asm(f"""
            lea rax, [rip + {control - (va + 7):#x}]
            movzx edx, byte ptr [rax]
            and edx, {HOLD_START | START_NOW}
            cmp edx, {HOLD_START}
            jne {rs['start']:#x}
            mov byte ptr [rax + {state + HELD - control:#x}], 1
            ret
        """, va)

    gate = p.add_cave(build_gate)
    site = rs["move_call"]
    p.write(site, p.asm(f"call {gate:#x}", site), expect=p.asm(f"call {rs['start']:#x}", site))

    # the roller opens nothing while a tick runs as playing over an open pick or the pause menu
    roller = rs["roller"]
    choice_roll = None
    if choice_g:
        def original_roll(va):
            code = rs['prologue']
            return code + p.asm(f"jmp {roller + len(code):#x}", va + len(code))
        trampoline = p.add_cave(original_roll)
        choice_roll = p.add_cave(lambda va: p.asm(choice.roller_code(choice_g, trampoline, va), va))
        p.meta['choice_roller_hook'] = choice_roll - p.ib

    def build_roll(va):
        return p.asm(f"""
            lea rax, [rip + {state + FAKED - (va + 7):#x}]
            cmp byte ptr [rax], 0
            je roll
            xor eax, eax
            ret
        roll:
            {f'jmp {choice_roll:#x}' if choice_roll else f'push rbp; push r15; push r14; jmp {roller + len(rs["prologue"]):#x}'}
        """, va)

    roll = p.add_cave(build_roll)
    p.write(roller, p.asm(f"jmp {roll:#x}", roller), expect=rs["prologue"])
    p.log.append(f"coop: run sync, tick call {rs['tick_call']:x}, run start {rs['move_call']:x}, roller {roller:x}")


# --- boss sync: bosses chase the nearer hero and share their health -------------------------------
# enemy damage's death branch: mov dword [rcx+dead], 1 ; movss [rcx+died at], xmm8 ; mov qword [rcx+0x10], 0 ;
# cmp qword [rcx+boss], -1 ; je ; mov r12, [rcx+..] ; jmp ; xor r12d, r12d ; mov r13/r14, [rcx+guardian] ;
# cmp r13/r14, -1 ; je ; mov rax, [rcx+lair index]
BOSS_KINDS = re.compile(rb"\xc7\x81(....)\x01\x00\x00\x00\xf3\x44\x0f\x11\x81....\x48\xc7\x41\x10\x00\x00\x00\x00"
                        rb"\x48\x83\x79(.)\xff\x74\x06\x4c\x8b\x61.\xeb\x03\x45\x31\xe4"
                        rb"\x4c\x8b([\x69\x71])(.)\x49\x83([\xfd\xfe])\xff\x74.\x48\x8b\x81(....)", re.S)
# the enemy AI caches my hero's position at its entry: movsd xmm0, [rcx+hero] ; movaps [rbp+xy], xmm0 ;
# movss xmm0, [rcx+hero+8] ; movss [rbp+z], xmm0
AI_HERO = re.compile(rb"\xf2\x0f\x10\x81(....)\x0f\x29(\x45.|\x85....)\xf3\x0f\x10\x81(....)\xf3\x0f\x11\x85(....)", re.S)
# its loop over the records: mov r8, [rbp+cur] ; mov rcx, [rbp+index] ; cmp r8, [rbp+end] ; setne al ; je ;
# mov rdx, rcx ; lea rcx, [r8+size] ; mov [rbp+cur], rcx ; mov [rbp+this index], rdx ; lea rcx, [rdx+1] ;
# mov [rbp+index], rcx ; cmp byte [r8+dead], 0
AI_LOOP = re.compile(rb"\x4c\x8b\x85(....)\x48\x8b\x8d(....)\x4c\x3b(\x45.|\x85....)\x0f\x95\xc0\x0f\x84....\x48\x89\xca"
                     rb"\x49\x8d\x88(....)\x48\x89\x8d(....)\x48\x89\x95(....)\x48\x8d\x4a\x01\x48\x89\x8d(....)"
                     rb"\x41\x80\xb8(....)\x00", re.S)
# its enrage check: movss xmm0, [r14+hp] ; movss xmm1, [r14+max hp] ; movaps xmm2, xmm1 ; mulss xmm2, [..]
AI_HP = re.compile(rb"\xf3\x41\x0f\x10\x86(....)\xf3\x41\x0f\x10\x8e(....)\x0f\x28\xd1\xf3\x0f\x59\x15", re.S)
# what a record's turn queues against my hero, Vecs {cap, ptr, len} on its stack, by their pushes:
# mov rax, [rbp+ptr] ; ... ; inc rsi ; mov [rbp+len], rsi. Melee hits {f32 damage, guid}, casts at me
# {guid, u32 spell, f32}, abilities (entries 0x30 / 0x38 bytes, first the record's index)
AI_MELEE = re.compile(rb"\x48\x8b\x85(....)\x48\x89\xf1\x48\xc1\xe1\x04\xf3\x44\x0f\x11[\x04-\x3c]\x08"
                      rb"\x48\x89\x7c\x08\x08\x48\xff\xc6\x48\x89\xb5(....)", re.S)
AI_CAST = re.compile(rb"\x48\x8b\x85(....)\x48\x89\xf1\x48\xc1\xe1\x04\x48\x89\x1c\x08\x89\x7c\x08\x08"
                     rb"\xf3\x44\x0f\x11[\x44-\x7c]\x08\x0c\x48\xff\xc6\x48\x89\xb5(....)", re.S)
AI_ABILITY = re.compile(rb"\x48\x8b\x85(....)(?:\x48\x6b\xce.|\x48\x8d\x0c\x76\x48\xc1\xe1\x04)\x48\x8b\x95(....)"
                        rb"\x48\x89\x14\x08", re.S)
# chasers walk the flow field the AI builds toward my hero (once a pass, an Arc on its stack) unless it is
# null: cmp qword [rbp+nav grid], -1 ; je ; mov rdx, [rbp+field] ; test rdx, rdx ; je ; add rdx, 0x10
AI_FIELD = re.compile(rb"\x48\x83\xbd(....)\xff\x0f\x84....\x48\x8b\x95(....)\x48\x85\xd2\x0f\x84....\x48\x83\xc2\x10", re.S)


def rbp_slot(b):
    """The displacement of an [rbp+disp] operand from its modrm on: 0x45 disp8 or 0x85 disp32."""
    return struct.unpack("<b", b[1:2])[0] if b[0] == 0x45 else s32(b[1:5])


def find_boss_sync(p, full, enemy_damage, tick_fn):
    """Boss records (the death branch's boss / guardian fields, the lair index), the enemy AI's
    record loop, its cached hero position and the stack Vecs a turn fills against my hero, all read
    from the game code and cross-checked; raises PatchError when the game changed."""
    lo, hi = (p.off(v) for v in func_range(p, enemy_damage))
    kinds = list(BOSS_KINDS.finditer(p.d, lo, hi))
    p.check(len(kinds) == 1, f"boss sync: {len(kinds)} death branches")
    k = kinds[0]
    p.check(k.group(3)[0] >> 3 & 7 == k.group(5)[0] & 7, "boss sync: guardian check reads another register")
    hero = one(p, AI_HERO, "enemy AI hero position")
    ai = func_range(p, p.va(hero.start()))
    tick_end = func_range(p, tick_fn) if tick_fn else None
    p.check(ai is not None and tick_end is not None, "boss sync: enemy AI or run tick outside .pdata")
    ai_lo, ai_hi = ai
    lo, hi = p.off(ai_lo), p.off(ai_hi)
    loops = list(AI_LOOP.finditer(p.d, lo, hi))
    hps = list(AI_HP.finditer(p.d, lo, hi))
    p.check(len(loops) == 1 and len(hps) == 1, f"boss sync: AI loop {len(loops)}, enrage check {len(hps)}")
    loop, hp = loops[0], hps[0]
    g = [s32(loop.group(i)) if i != 3 else rbp_slot(loop.group(3)) for i in range(1, 9)]
    cur, idx, end, size, cur2, turn, idx2, dead = g
    p.check(cur == cur2 and idx == idx2 and size == full["size"] and dead == s32(k.group(1))
            and s32(hero.group(3)) == s32(hero.group(1)) + 8 and s32(hp.group(1)) == full["hp"],
            "boss sync: enemy AI layout disagrees")
    calls = [m for m in re.finditer(rb"\xe8(....)", p.d[p.off(tick_fn):p.off(tick_end[1])], re.S)
             if tick_fn + m.end() + s32(m.group(1)) == ai_lo]
    p.check(len(calls) == 1, f"boss sync: the run tick calls the enemy AI {len(calls)} times")
    lens = []
    for what, pattern in (("melee", AI_MELEE), ("cast", AI_CAST), ("ability", AI_ABILITY)):
        found = {(s32(m.group(1)), s32(m.group(2))) for m in pattern.finditer(p.d, lo, hi)}
        p.check(len(found) == 1, f"boss sync: AI {what} list {found}")
        ptr, second = found.pop()
        p.check(second == (turn if pattern is AI_ABILITY else ptr + 8)
                and b"\x48\x89\xb5" + struct.pack("<i", ptr + 8) in p.d[lo:hi],  # mov [rbp+len], rsi
                f"boss sync: AI {what} list disagrees")
        lens.append(ptr + 8)
    fields = list(AI_FIELD.finditer(p.d, lo, hi))
    p.check(len(fields) == 1, f"boss sync: AI flow field {len(fields)} uses")
    field = s32(fields[0].group(2))
    p.check(b"\x48\x89\x85" + fields[0].group(2) in p.d[lo:loop.start()] and len(set(lens + [field])) == 4,
            "boss sync: AI lists or flow field disagree")
    return {"dead": dead, "boss": k.group(2)[0], "guard": k.group(4)[0], "lair": s32(k.group(6)),
            "max": s32(hp.group(2)), "loop": p.va(loop.start()), "head": loop.group(0)[:7], "cur": cur, "idx": idx,
            "end": end, "xy": rbp_slot(hero.group(2)), "z": s32(hero.group(4)), "lens": lens, "field": field,
            "hero": s32(hero.group(1))}


def is_boss(reg, b, no):
    """Jumps to `no` unless the enemy record at reg is a boss or a guardian."""
    return f"""
        cmp qword ptr [{reg} + {b['boss']:#x}], -1
        jne {no}_boss
        cmp qword ptr [{reg} + {b['guard']:#x}], -1
        je {no}
    {no}_boss:
    """


def hook_boss_chase(p, b, full, state, sh=None, choice_g=None):
    """At each turn of the enemy AI's record loop: a boss nearer to the ally than to my hero (both from
    where it is heading) chases the ally's position, cached for that turn in place of my hero's, walking
    straight to it (the flow field leads to my hero, so it is hidden for that turn), and the melee hits,
    casts and abilities its turn queued are dropped at the next turn; everything else chases my hero.
    Inert unless the tick found the boss sync live. With the shared enemies (sh) a record the shared table
    gave an owner (set by the shared tick, checked against its guid; bosses too) is lured exactly when that
    owner is the partner, while luring is ok."""
    site = b["loop"]
    saved = b["lens"] + [b["field"]]
    keep = lambda i, l: f"""
        mov rcx, qword ptr [rbp + {l:#x}]
        mov qword ptr [rax + {LURED_SLOTS + 8 * i}], rcx
    """
    drop = lambda i, l: f"""
        mov rcx, qword ptr [rax + {LURED_SLOTS + 8 * i}]
        mov qword ptr [rbp + {l:#x}], rcx
    """
    dist = lambda xy, tag: f"""
        movsd xmm{tag}, qword ptr [{xy}]
        movsd xmm2, qword ptr [rcx + {full['to']:#x}]
        subps xmm{tag}, xmm2
        mulps xmm{tag}, xmm{tag}
        movshdup xmm2, xmm{tag}
        addss xmm{tag}, xmm2
    """

    owned = "" if not sh else f"""
            cmp byte ptr [rax + {sh['sh'] + shared.LURE_OK - state:#x}], 0
            je bc_boss
            mov dx, word ptr [rcx + {full['guid']:#x}]
            cmp dx, word ptr [rcx + {sh['owner'] + 1:#x}]
            jne bc_boss
            cmp byte ptr [rcx + {sh['owner']:#x}], 1
            je bc_lure
            {f'test byte ptr [rcx + {sh["owner"]:#x}], 2; jnz bc_hold' if choice_g else ''}
            jmp bc_done
        bc_boss:
    """

    hold = "" if not choice_g or not sh else f"""
            jmp bc_done
        bc_hold:
            cmp dword ptr [rax + {choice_g['choice'] + choice.ENABLED - state:#x}], 0
            je bc_hold_release
            test byte ptr [rax + {choice_g['choice'] + choice.EFFECTIVE_PEER - state:#x}], {choice.LIVE}
            jz bc_hold_release
            test byte ptr [rax + {choice_g['choice'] + choice.FLAGS - state:#x}], {choice.BOTH}
            jnz bc_hold_release
            cmp qword ptr [rcx + {b['boss']:#x}], -1
            jne bc_done
            cmp qword ptr [rcx + {b['guard']:#x}], -1
            jne bc_done
            test byte ptr [rcx + {sh['owner']:#x}], 0x80
            jnz bc_held
            or byte ptr [rcx + {sh['owner']:#x}], 0x80
            mov rdx, rcx
            mov rcx, qword ptr [rax + {STATE_PTR}]
            sub rsp, 0x28
            call {choice_g['hold']:#x}
            add rsp, 0x28
        bc_held:
            add qword ptr [rbp + {b['cur']:#x}], {full['size']:#x}
            inc qword ptr [rbp + {b['idx']:#x}]
            movups xmm0, xmmword ptr [rsp]
            movups xmm1, xmmword ptr [rsp + 0x10]
            movups xmm2, xmmword ptr [rsp + 0x20]
            add rsp, 0x30
            pop rdx
            pop rcx
            pop rax
            jmp {site:#x}
        bc_hold_release:
            and byte ptr [rcx + {sh['owner']:#x}], 0x7f
    """

    def build(va):
        # flags are dead here (the loop's cmp follows); every register is kept
        pre = p.asm("""
            push rax
            push rcx
            push rdx
            sub rsp, 0x30
            movups xmmword ptr [rsp], xmm0
            movups xmmword ptr [rsp + 0x10], xmm1
            movups xmmword ptr [rsp + 0x20], xmm2
        """, va)
        at = va + len(pre)
        body = p.asm(f"""
            lea rax, [rip + {state - (at + 7):#x}]
            cmp qword ptr [rbp + {b['idx']:#x}], 0
            jne bc_turn
            mov rcx, qword ptr [rbp + {b['xy']:#x}]
            mov qword ptr [rax + {HERO_XY}], rcx
            mov ecx, dword ptr [rbp + {b['z']:#x}]
            mov dword ptr [rax + {HERO_Z}], ecx
            jmp bc_mine
        bc_turn:
            cmp byte ptr [rax + {LURED}], 0
            je bc_mine
            {"".join(drop(i, l) for i, l in enumerate(saved))}
        bc_mine:
            mov byte ptr [rax + {LURED}], 0
            mov rcx, qword ptr [rax + {HERO_XY}]
            mov qword ptr [rbp + {b['xy']:#x}], rcx
            mov ecx, dword ptr [rax + {HERO_Z}]
            mov dword ptr [rbp + {b['z']:#x}], ecx
            mov rcx, qword ptr [rbp + {b['cur']:#x}]
            cmp rcx, qword ptr [rbp + {b['end']:#x}]
            je bc_done
            cmp byte ptr [rcx + {b['dead']:#x}], 0
            jne bc_done
            {owned}
            cmp byte ptr [rax + {BOSS_LIVE}], 0
            je bc_done
            {is_boss('rcx', b, 'bc_done')}
            {dist(f"rax + {HERO_XY}", 0)}
            {dist(f"rax + {snap(B_POS)}", 1)}
            ucomiss xmm1, xmm0
            jae bc_done
        bc_lure:
            mov rcx, qword ptr [rax + {snap(B_POS)}]
            mov qword ptr [rbp + {b['xy']:#x}], rcx
            mov ecx, dword ptr [rax + {snap(B_POS) + 8}]
            mov dword ptr [rbp + {b['z']:#x}], ecx
            {"".join(keep(i, l) for i, l in enumerate(saved))}
            mov qword ptr [rbp + {b['field']:#x}], 0
            mov byte ptr [rax + {LURED}], 1
            {hold}
        bc_done:
            movups xmm0, xmmword ptr [rsp]
            movups xmm1, xmmword ptr [rsp + 0x10]
            movups xmm2, xmmword ptr [rsp + 0x20]
            add rsp, 0x30
            pop rdx
            pop rcx
            pop rax
        """, at)
        back = at + len(body) + len(b["head"])
        return pre + body + b["head"] + p.asm(f"jmp {site + len(b['head']):#x}", back)

    cave = p.add_cave(build)
    p.write(site, p.asm(f"jmp {cave:#x}", site).ljust(len(b["head"]), b"\x90"), expect=b["head"])
    p.log.append(f"coop: boss sync, AI loop {site:x}, lists {[hex(l) for l in b['lens']]}, flow field {b['field']:#x}"
                 + (", shared enemies lured by owner" if sh else ""))


def patch(p):
    tick_va = p.offsets.find_unique('run.tick_site',
        ['0F 28 F1 ?? ?? ?? 0F B6 81 ?? ?? ?? ?? 84 C0'],
        validator=lambda va: TICK.match(p.d, p.off(va)) is not None)
    tick = TICK.match(p.d, p.off(tick_va))
    phase = p.offsets.field('RunState', 'phase', [p.va(tick.start(2) - 3)], base='rcx', width=1)
    clocks = [m for m in CLOCK.finditer(p.d, tick.end(), tick.end() + 0x80)]
    p.check(len(clocks) == 1, "coop ghost: run clock not found after the tick entry")
    clock = p.offsets.field('RunState', 'clock', [p.va(clocks[0].start())], base='r13', width=4)
    queue = find_queue(p)
    queue_sites = [p.va(m.start() + 7) for m in QUEUE.finditer(p.d)
                   if struct.unpack('<i', m.group(2))[0] == queue]
    p.check(p.offsets.field('RunState', 'queue', queue_sites, base='r13', width=8) == queue,
            'coop: queue field disagrees with validated push')
    alloc, grow = find_alloc(p), find_grow(p, queue)
    for name, address in [('packet.alloc', alloc), ('packet.grow', grow)]:
        p.offsets.symbol(name, address, evidence='packet allocation and queue layout cross-checks')
    name_sites = find_name_replies(p)
    try:
        hero_damage, hero_hp = find_hero_damage(p, phase)
        # cross-check: the tick's no-offers branch right after the clock refills it (movss [r13+hp], xmm0)
        p.check(b"\xf3\x41\x0f\x11\x85" + struct.pack("<i", hero_hp) in p.d[clocks[0].end():clocks[0].end() + 0x40],
                f"coop: hero health {hero_hp:#x} not confirmed by the run tick")
        hp_at = p.d.find(b"\xf3\x41\x0f\x11\x85" + struct.pack('<i', hero_hp),
                         clocks[0].end(), clocks[0].end() + 0x40)
        p.check(p.offsets.field('RunState', 'hero_hp', [p.va(hp_at)], base='r13', width=4) == hero_hp,
                'coop: hero health field disagrees')
        p.offsets.symbol('run.hero_damage', hero_damage, evidence='death branch and tick health cross-check')
    except Exception as e:  # only END_RUN_DEFEAT depends on it
        if type(e).__name__ != "PatchError":
            raise
        hero_damage = hero_hp = None
        p.log.append(f"coop: END_RUN_DEFEAT disabled ({e})")
    ally = bool(name_sites)
    enemy_damage = find_enemy_damage(p, phase) if ally else None
    if enemy_damage:
        p.offsets.symbol('run.enemy_damage', enemy_damage, evidence='phase guard and runtime function bounds')
    slots = find_damage_slots(p, enemy_damage, phase, queue) if ally else None
    enemy_query = find_enemy_query(p, clock) if ally else None
    dealloc = find_dealloc(p) if ally else None
    full = None
    if ally:
        try:
            full = find_sync(p, phase, clock, enemy_damage)
        except Exception as e:  # the ally then keeps the dps estimate and picks stay personal
            if type(e).__name__ != "PatchError":
                raise
            p.log.append(f"coop: full sync disabled ({e})")
    stats = None
    if full:
        try:
            stats = find_damage_stats(p, enemy_damage)
        except Exception as e:  # replayed hits then count in my damage stats
            if type(e).__name__ != "PatchError":
                raise
            p.log.append(f"coop: replays count in my damage stats ({e})")
    try:
        run_sync = find_run_sync(p, tick, phase)
    except Exception as e:  # co-op then keeps the stock pauses and run start
        if type(e).__name__ != "PatchError":
            raise
        run_sync = None
        p.log.append(f"coop: run sync disabled ({e})")
    boss = None
    if full:
        try:
            boss = find_boss_sync(p, full, enemy_damage, func_start(p, p.va(tick.start())))
        except Exception as e:  # bosses then stay solo on each screen
            if type(e).__name__ != "PatchError":
                raise
            p.log.append(f"coop: boss sync disabled ({e})")
    sh = None
    if boss:
        try:
            sh = shared.find(p, full, boss, enemy_damage, func_range)
        except Exception as e:  # each screen then keeps its own enemies
            if type(e).__name__ != "PatchError":
                raise
            p.log.append(f"coop: shared enemies disabled ({e})")
    else:
        p.log.append("coop: shared enemies disabled (no boss sync)")
    announce = find_announce(p) if hero_damage else None
    guid = ALLY_GUID if ally else GHOST_GUID

    create, c_off = create_packet(ally)
    move, m_off = move_packet(guid)
    destroy = guid.to_bytes(8, "little")
    block = p.add_data(BLOCK_SIZE, b"AZSGHST3")
    state = p.add_data(STATE_SIZE)
    consts = p.add_data(64, struct.pack("<16f", MIN_INTERVAL, TIMEOUT, 1000.0, 60.0, 400.0, LETHAL,
                                        ATTACK_INTERVAL, MAX_WINDOW, RADIUS, MAX_DPS, DEFEAT_DELAY, MAP_RADIUS,
                                        NOTE_EVERY, BOSS_EPS, 1.0, 1e-6))
    spells = p.add_data(8 * 12, b"".join(struct.pack("<II", *CLASS_SPELLS.get(c, DEFAULT_SPELL))
                                         for c in range(12)))
    # SMSG_SPELL_GO head (full["go"]): caster and caster again, spell, cast flags 0x100, 1 hit
    pg_ally = packed_guid(ALLY_GUID)
    go_head = pg_ally + pg_ally + struct.pack("<IHB", 0, 0x100, 1)
    go_spell = 2 * len(pg_ally)
    t_go = p.add_data(len(go_head), go_head)
    displays = p.add_data(4 * len(DISPLAYS), struct.pack(f"<{len(DISPLAYS)}I", *DISPLAYS))
    t_create = p.add_data(len(create), create)
    t_move = p.add_data(len(move), move)
    t_destroy = p.add_data(len(destroy), destroy)
    # per-summon templates, laid out back to back so index i is base + i * stride
    s_creates = [create_packet(False, SUMMON_GUID + i) for i in range(SUMMONS)]
    s_moves = [move_packet(SUMMON_GUID + i) for i in range(SUMMONS)]
    s_offs, sm_off = s_creates[0][1], s_moves[0][1]
    t_screate = p.add_data(sum(len(c) for c, _ in s_creates), b"".join(c for c, _ in s_creates))
    t_smove = p.add_data(sum(len(m) for m, _ in s_moves), b"".join(m for m, _ in s_moves))
    t_sdestroy = p.add_data(8 * SUMMONS, b"".join((SUMMON_GUID + i).to_bytes(8, "little") for i in range(SUMMONS)))
    sc_len, sm_len = len(s_creates[0][0]), len(s_moves[0][0])
    p.check(all(len(c) == sc_len for c, _ in s_creates) and all(len(m) == sm_len for m, _ in s_moves),
            "coop: summon templates differ in size")
    s_displays = p.add_data(4 * len(SUMMON_DISPLAYS), struct.pack(f"<{len(SUMMON_DISPLAYS)}I", *SUMMON_DISPLAYS))
    # announcement texts per language: {prefix offset, prefix len, suffix offset, suffix len} from block
    # message m in language l is entry m * 2 + l: 0 the ally has fallen, 1 waiting for the host
    texts = b"".join(a + b for a, b in MESSAGES + WAIT_MESSAGES)
    t_texts = p.add_data(len(texts), texts)
    table, at = b"", t_texts - block
    for prefix, suffix in MESSAGES + WAIT_MESSAGES:
        table += struct.pack("<IIII", at, len(prefix), at + len(prefix), len(suffix))
        at += len(prefix) + len(suffix)
    t_msgtab = p.add_data(len(table), table)
    dead = death_packet()
    t_dead = p.add_data(len(dead), dead)
    health, h_off = values_packet({0x16: 100, 0x1C: 100, 0x22: 1})  # health, max health, level
    t_health = p.add_data(len(health), health)
    no_party = bytes(14)  # SMSG_GROUP_LIST: type, own flags, 0 members, leader 0
    t_no_party = p.add_data(len(no_party), no_party)
    hits = p.add_data(HITS_BOSS + 8 * BOSS_SLOTS, b"AZSHITS1") if full else None
    if full:
        p.meta["hits_rva"] = hits - p.ib
    if boss:
        p.meta["boss_sync"] = 1
    p.meta["ghost_rva"] = block - p.ib
    p.meta["ghost_size"] = BLOCK_SIZE
    p.meta["ghost_magic"] = "AZSGHST3"
    p.meta["ghost_kind"] = "ally" if ally else "ghost"
    if run_sync:
        p.meta["ghost_sync"] = 1
    p.log.append(f"coop: {p.meta['ghost_kind']}, {len(name_sites)} name reply site(s)")
    shared_tick = ""
    if sh:
        sh["control"] = block + B_CONTROL
        g = {"block": block, "state": state, "B_CONTROL": B_CONTROL, "timeout": TIMEOUT, "full": full, "clock": clock,
             "phase": phase, "hero": boss["hero"], "queue": queue, "grow": grow, "enemy_damage": enemy_damage,
             "spells": spells, "SPAWNED": SPAWNED, "ALLY_DEAD": ALLY_DEAD, "CLOCK_NOW": CLOCK_NOW, "LAST_UPD": LAST_UPD,
             "REPLAYING": REPLAYING, "OVERRIDE": OVERRIDE, "SAVED_KIND": SAVED_KIND, "TARGET": TARGET,
             "REPLAY_DMG": REPLAY_DMG, "SPELL_ID": SPELL_ID, "B_CLASS": snap(B_CLASS), "BOSS_GUIDS": BOSS_GUIDS,
             "BOSS_KEYS": BOSS_KEYS, "BOSS_SLOTS": BOSS_SLOTS}
        shared_tick = f"""
            mov rcx, r13
            call {shared.install(p, sh, g):#x}
        """

    def send(tag, template, size, opcode, fill="", ok=""):
        """Allocates a packet buffer, copies the template, lets `fill` patch it (buffer in rbx),
        pushes it to the outbound queue and runs `ok`. Skips all of it if the allocation fails."""
        return f"""
            mov ecx, {size}
            mov edx, 1
            call {alloc:#x}
            test rax, rax
            jz {tag}_skip
            mov rbx, rax
            mov rdi, rax
            {f"lea rsi, [r15 + {template - block:#x}]" if template else f"mov rsi, qword ptr [r14 + {SUM_TPL}]"}
            mov ecx, {size}
            rep movsb
            {fill}
            lea rcx, [r13 + {queue:#x}]
            mov rax, qword ptr [rcx + 0x10]
            cmp rax, qword ptr [rcx]
            jne {tag}_room
            call {grow:#x}
            lea rcx, [r13 + {queue:#x}]
            mov rax, qword ptr [rcx + 0x10]
        {tag}_room:
            shl rax, 5
            add rax, qword ptr [rcx + 8]
            mov qword ptr [rax], {size}
            mov qword ptr [rax + 8], rbx
            mov qword ptr [rax + 0x10], {size}
            mov qword ptr [rax + 0x18], {opcode:#x}
            inc qword ptr [rcx + 0x10]
            {ok}
        {tag}_skip:
        """

    def build_tick(va):
        display = f"""
            mov eax, {DEFAULT_DISPLAY}
            mov ecx, dword ptr [r14 + {snap(B_RACE)}]
            cmp ecx, {len(DISPLAYS) // 2 - 1}
            ja disp_ok
            mov edx, dword ptr [r14 + {snap(B_GENDER)}]
            and edx, 1
            lea ecx, [rcx * 2 + rdx]
            lea rdx, [r15 + {displays - block:#x}]
            mov ecx, dword ptr [rdx + rcx * 4]
            test ecx, ecx
            cmovnz eax, ecx
        disp_ok:
        """
        hint = "" if ally else f"""
            mov ecx, dword ptr [r14 + {snap(B_HINT)}]
            test ecx, ecx
            cmovnz eax, ecx
        """
        looks = "" if not ally else f"""
            mov eax, dword ptr [r14 + {snap(B_LEVEL)}]
            mov ecx, 1
            test eax, eax
            cmovz eax, ecx
            mov dword ptr [rbx + {c_off['level']}], eax
            mov eax, dword ptr [r14 + {snap(B_LOOK)}]
            mov dword ptr [rbx + {c_off['pbytes']}], eax
            movzx eax, byte ptr [r14 + {snap(B_FACIAL)}]
            or eax, 0x2000000
            mov dword ptr [rbx + {c_off['pbytes2']}], eax
            movzx eax, byte ptr [r14 + {snap(B_GENDER)}]
            mov dword ptr [rbx + {c_off['pbytes3']}], eax
            lea rsi, [r14 + {snap(B_ITEMS)}]
            lea rdi, [rbx + {c_off['items']}]
            mov ecx, {SLOTS * 4}
            rep movsb
        """
        create_fill = f"""
            mov rax, qword ptr [r14 + {snap(B_POS)}]
            mov qword ptr [rbx + {c_off['pos']}], rax
            mov rax, qword ptr [r14 + {snap(B_POS) + 8}]
            mov qword ptr [rbx + {c_off['pos'] + 8}], rax
            movzx eax, byte ptr [r14 + {snap(B_RACE)}]
            movzx ecx, byte ptr [r14 + {snap(B_CLASS)}]
            shl ecx, 8
            or eax, ecx
            movzx ecx, byte ptr [r14 + {snap(B_GENDER)}]
            shl ecx, 16
            or eax, ecx
            mov dword ptr [rbx + {c_off['bytes0']}], eax
            {display}
            {hint}
            mov dword ptr [rbx + {c_off['display']}], eax
            mov dword ptr [rbx + {c_off['native']}], eax
            {looks}
        """
        move_fill = f"""
            mov rax, qword ptr [r14 + {LAST_POS}]
            mov qword ptr [rbx + {m_off['start']}], rax
            mov eax, dword ptr [r14 + {LAST_POS + 8}]
            mov dword ptr [rbx + {m_off['start'] + 8}], eax
            inc dword ptr [r14 + {SPLINE}]
            mov eax, dword ptr [r14 + {SPLINE}]
            mov dword ptr [rbx + {m_off['spline']}], eax
            mov dword ptr [rbx + {m_off['duration']}], r12d
            mov rax, qword ptr [r14 + {snap(B_POS)}]
            mov qword ptr [rbx + {m_off['dest']}], rax
            mov eax, dword ptr [r14 + {snap(B_POS) + 8}]
            mov dword ptr [rbx + {m_off['dest'] + 8}], eax
        """
        keep_pos = f"""
            mov rax, qword ptr [r14 + {snap(B_POS)}]
            mov qword ptr [r14 + {LAST_POS}], rax
            mov eax, dword ptr [r14 + {snap(B_POS) + 8}]
            mov dword ptr [r14 + {LAST_POS + 8}], eax
            mov eax, dword ptr [r14 + {CLOCK_NOW}]
            mov dword ptr [r14 + {LAST_MOVE}], eax
            mov byte ptr [r14 + {NEW}], 0
        """
        moved = f"""
            mov rax, qword ptr [r14 + {LAST_POS}]
            mov qword ptr [r14 + {PREV_POS}], rax
            mov eax, dword ptr [r14 + {LAST_POS + 8}]
            mov dword ptr [r14 + {PREV_POS + 8}], eax
            mov dword ptr [r14 + {DUR}], r12d
            mov byte ptr [r14 + {MOVED}], 1
        """
        # summon i of the ally: offset (dx, dy) of entry i -> rdx = &entry
        entry = f"""
            mov ecx, dword ptr [r14 + {SUM_I}]
            imul edx, ecx, 12
            lea rdx, [r14 + rdx + {snap(B_SUMMONS)}]
        """
        tpl = lambda base, stride: f"""
            mov ecx, dword ptr [r14 + {SUM_I}]
            imul ecx, ecx, {stride}
            lea rax, [r15 + {base - block:#x}]
            add rax, rcx
            mov qword ptr [r14 + {SUM_TPL}], rax
        """
        at_offset = lambda base, out: f"""
            {entry}
            movss xmm0, dword ptr [r14 + {base}]
            addss xmm0, dword ptr [rdx + 4]
            movss dword ptr [rbx + {out}], xmm0
            movss xmm0, dword ptr [r14 + {base + 4}]
            addss xmm0, dword ptr [rdx + 8]
            movss dword ptr [rbx + {out + 4}], xmm0
            mov eax, dword ptr [r14 + {base + 8}]
            mov dword ptr [rbx + {out + 8}], eax
        """
        sum_create_fill = f"""
            {at_offset(LAST_POS, s_offs['pos'])}
            mov eax, dword ptr [r14 + {SUM_WANT}]
            lea rdx, [r15 + {s_displays - block:#x}]
            mov eax, dword ptr [rdx + rax * 4]
            mov dword ptr [rbx + {s_offs['display']}], eax
            mov dword ptr [rbx + {s_offs['native']}], eax
        """
        sum_move_fill = f"""
            {at_offset(PREV_POS, sm_off['start'])}
            {at_offset(LAST_POS, sm_off['dest'])}
            inc dword ptr [r14 + {SPLINE}]
            mov eax, dword ptr [r14 + {SPLINE}]
            mov dword ptr [rbx + {sm_off['spline']}], eax
            mov eax, dword ptr [r14 + {DUR}]
            mov dword ptr [rbx + {sm_off['duration']}], eax
        """
        set_kind = lambda value: f"""
            mov ecx, dword ptr [r14 + {SUM_I}]
            mov eax, {value}
            mov byte ptr [r14 + rcx + {SUM_KIND}], al
        """
        # keeps the mirrored summons in line with the snapshot: destroy changed ones, create
        # missing ones (while playing), move the rest along with the ally; a local subroutine
        sum_sync = "" if not ally else f"""
        sum_sync:
            sub rsp, 8
            mov dword ptr [r14 + {SUM_I}], 0
        sum_loop:
            mov ecx, dword ptr [r14 + {SUM_I}]
            cmp ecx, {SUMMONS}
            jae sum_end
            xor eax, eax
            cmp ecx, dword ptr [r14 + {snap(B_SUMMON_COUNT)}]
            jae sum_kind
            imul edx, ecx, 12
            mov eax, dword ptr [r14 + rdx + {snap(B_SUMMONS)}]
            cmp eax, {len(SUMMON_DISPLAYS) - 1}
            jbe sum_kind
            xor eax, eax
        sum_kind:
            mov dword ptr [r14 + {SUM_WANT}], eax
            movzx edx, byte ptr [r14 + rcx + {SUM_KIND}]
            test edx, edx
            jz sum_create
            cmp edx, eax
            je sum_move
            {tpl(t_sdestroy, 8)}
            {send('sdes', None, 8, 0xAA, ok=set_kind(0))}
            mov ecx, dword ptr [r14 + {SUM_I}]
            cmp byte ptr [r14 + rcx + {SUM_KIND}], 0
            jne sum_next
        sum_create:
            cmp dword ptr [r14 + {SUM_WANT}], 0
            je sum_next
            cmp byte ptr [r13 + {phase:#x}], 1
            jne sum_next
            {tpl(t_screate, sc_len)}
            {send('scre', None, sc_len, 0xA9, sum_create_fill, ok=set_kind(f"dword ptr [r14 + {SUM_WANT}]"))}
            jmp sum_next
        sum_move:
            cmp byte ptr [r14 + {MOVED}], 0
            je sum_next
            {tpl(t_smove, sm_len)}
            {send('smov', None, sm_len, 0xDD, sum_move_fill)}
        sum_next:
            inc dword ptr [r14 + {SUM_I}]
            jmp sum_loop
        sum_end:
            mov byte ptr [r14 + {MOVED}], 0
            add rsp, 8
            ret
        """
        sync = "" if not ally else "call sum_sync"
        drop_summons = "" if not ally else f"""
            mov dword ptr [r14 + {snap(B_SUMMON_COUNT)}], 0
            call sum_sync
        """
        gone = f"mov dword ptr [r14 + {SPAWNED}], 0"
        party_leave = "" if not ally else f"""
            cmp byte ptr [r14 + {IN_PARTY}], 0
            je pl_end_TAG
            {send('pl_TAG', t_no_party, len(no_party), SMSG_GROUP_LIST, ok=f"mov byte ptr [r14 + {IN_PARTY}], 0")}
        pl_end_TAG:
        """
        # the ally appears only with a nick in the same snapshot, so the client never caches the
        # hero's name (the stock name reply) for the ally guid
        wait_for_nick = "" if not ally else f"""
            cmp byte ptr [r14 + {snap(B_NICK)}], 0
            je done
        """
        def compose(message, tag, nick):
            """Message `message` in the block's language into MSG (the partner's nick between its
            prefix and suffix if `nick`); length -> rdi."""
            copy_nick = "" if not nick else f"""
            lea rsi, [r14 + {snap(B_NICK)}]
            mov ecx, {NICK_LEN}
        {tag}_nick:
            test ecx, ecx
            jz {tag}_nick_done
            mov al, byte ptr [rsi]
            test al, al
            jz {tag}_nick_done
            mov byte ptr [rdi], al
            inc rsi
            inc rdi
            dec ecx
            jmp {tag}_nick
        {tag}_nick_done:
            """
            return f"""
            movzx eax, byte ptr [r14 + {snap(B_LANG)}]
            test eax, eax
            setnz al
            shl eax, 4
            lea rdx, [r15 + {t_msgtab - block + 32 * message:#x}]
            add rdx, rax
            lea rdi, [r14 + {MSG}]
            mov eax, dword ptr [rdx]
            lea rsi, [r15 + rax]
            mov ecx, dword ptr [rdx + 4]
            rep movsb
            {copy_nick}
            mov eax, dword ptr [rdx + 8]
            lea rsi, [r15 + rax]
            mov ecx, dword ptr [rdx + 12]
            rep movsb
            lea rax, [r14 + {MSG}]
            sub rdi, rax
            """

        def announce_call(message, tag, nick):
            """The game's big centre-screen message, pushed into the outbound queue."""
            return "" if not announce else f"""
            {compose(message, tag, nick)}
            lea rcx, [rsp + 0x40]
            lea rdx, [r14 + {MSG}]
            xor r8d, r8d
            lea r9, [r14 + {MSG}]
            mov qword ptr [rsp + 0x20], rdi
            call {announce:#x}
            lea rcx, [r13 + {queue:#x}]
            mov rax, qword ptr [rcx + 0x10]
            cmp rax, qword ptr [rcx]
            jne {tag}_room
            call {grow:#x}
            lea rcx, [r13 + {queue:#x}]
            mov rax, qword ptr [rcx + 0x10]
        {tag}_room:
            shl rax, 5
            add rax, qword ptr [rcx + 8]
            movups xmm0, xmmword ptr [rsp + 0x40]
            movups xmmword ptr [rax], xmm0
            movups xmm0, xmmword ptr [rsp + 0x50]
            movups xmmword ptr [rax + 0x10], xmm0
            inc qword ptr [rcx + 0x10]
            """
        fall = "" if not ally else f"""
            cmp dword ptr [r14 + {SPAWNED}], 0
            je fallen
            {send('dead', t_dead, len(dead), 0xA9, ok=f"mov byte ptr [r14 + {ALLY_DEAD}], 1")}
        fallen:
        """
        # run sync: my run ends on the partner's time of death (the clocks ran together until then)
        end_on_partner = "" if not run_sync else f"""
            movss xmm0, dword ptr [r14 + {snap(B_END)}]
            xorps xmm1, xmm1
            ucomiss xmm0, xmm1
            jbe end_kept
            movss dword ptr [r13 + {clock:#x}], xmm0
            movss dword ptr [r14 + {CLOCK_NOW}], xmm0
            movss dword ptr [r14 + {LAST_CLOCK}], xmm0
            movss dword ptr [r14 + {DEFEAT_AT}], xmm0
        end_kept:
        """
        defeat = "" if not hero_damage else f"""
            test byte ptr [r14 + {snap(B_CONTROL)}], {END_RUN_DEFEAT}
            jz alive
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
            subss xmm0, dword ptr [r14 + {LAST_UPD}]
            ucomiss xmm0, dword ptr [r15 + {consts + 4 - block:#x}]
            ja alive
            movzx eax, byte ptr [r13 + {phase:#x}]
            cmp eax, 1
            jb alive
            cmp eax, 3
            ja alive
            cmp byte ptr [r14 + {ANNOUNCED}], 0
            jne announced
            mov byte ptr [r14 + {ANNOUNCED}], 1
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
            addss xmm0, dword ptr [r15 + {consts + 40 - block:#x}]
            movss dword ptr [r14 + {DEFEAT_AT}], xmm0
            {announce_call(0, 'ann', True)}
            {fall}
        announced:
            cmp byte ptr [r14 + {DEFEATED}], 0
            jne alive
            cmp byte ptr [r13 + {phase:#x}], 1
            jne alive
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
            ucomiss xmm0, dword ptr [r14 + {DEFEAT_AT}]
            jb alive
            {end_on_partner}
            mov rcx, r13
            movss xmm1, dword ptr [r13 + {hero_hp:#x}]
            addss xmm1, xmm1
            addss xmm1, dword ptr [r15 + {consts + 20 - block:#x}]
            xor r8d, r8d
            xor r9d, r9d
            mov byte ptr [r14 + {DEFEAT_HIT}], 1
            call {hero_damage:#x}
            mov byte ptr [r14 + {DEFEAT_HIT}], 0
            cmp byte ptr [r13 + {phase:#x}], 1
            je alive
            mov byte ptr [r14 + {DEFEATED}], 1
        """
        # the partner's health, max health and level on the ally (unit frames), whenever they change;
        # a partner that sends no max health (older companion) keeps the created 100 / 100
        level = f"""
            mov eax, dword ptr [r14 + {snap(B_LEVEL)}]
            mov ecx, 1
            test eax, eax
            cmovz eax, ecx
        """
        health_fill = f"""
            mov eax, dword ptr [r14 + {snap(B_HP)}]
            mov dword ptr [rbx + {h_off[0x16]}], eax
            mov eax, dword ptr [r14 + {snap(B_HPM)}]
            mov dword ptr [rbx + {h_off[0x1C]}], eax
            {level}
            mov dword ptr [rbx + {h_off[0x22]}], eax
        """
        health_sent = f"""
            mov eax, dword ptr [r14 + {snap(B_HP)}]
            mov dword ptr [r14 + {LAST_HP}], eax
            mov eax, dword ptr [r14 + {snap(B_HPM)}]
            mov dword ptr [r14 + {LAST_HPM}], eax
            {level}
            mov dword ptr [r14 + {LAST_LVL}], eax
        """
        health_sync = "" if not ally else f"""
            cmp dword ptr [r14 + {snap(B_HPM)}], 0
            je hp_end
            mov eax, dword ptr [r14 + {snap(B_HP)}]
            cmp eax, dword ptr [r14 + {LAST_HP}]
            jne hp_send
            mov eax, dword ptr [r14 + {snap(B_HPM)}]
            cmp eax, dword ptr [r14 + {LAST_HPM}]
            jne hp_send
            {level}
            cmp eax, dword ptr [r14 + {LAST_LVL}]
            je hp_end
        hp_send:
            {send('hp', t_health, len(health), 0xA9, health_fill, ok=health_sent)}
        hp_end:
        """
        def count(tag, field, seen, out):
            """hits block +out += how much the run's counter at state+field went up since the last tick."""
            return f"""
            mov eax, dword ptr [r13 + {field:#x}]
            mov ecx, eax
            sub ecx, dword ptr [r14 + {seen}]
            mov dword ptr [r14 + {seen}], eax
            jbe {tag}_end
            add dword ptr [r15 + {hits + out - block:#x}], ecx
        {tag}_end:
            """

        def grant(i, pending):
            """One more pending pick (state+pending) per tick while the partner's count i is ahead of
            what I granted; in the pre-run phase (0) or when the count went down: catch up silently."""
            return f"""
            mov eax, dword ptr [r14 + {snap(B_GRANTS) + 4 * i}]
            movzx ecx, byte ptr [r13 + {phase:#x}]
            test ecx, ecx
            jz gr{i}_sync
            sub eax, dword ptr [r14 + {GRANTED + 4 * i}]
            jz gr{i}_end
            js gr{i}_sync
            cmp ecx, 5
            ja gr{i}_end
            mov edx, 0x2e
            bt edx, ecx
            jnc gr{i}_end
            inc dword ptr [r13 + {pending:#x}]
            inc dword ptr [r14 + {GRANTED + 4 * i}]
            jmp gr{i}_end
        gr{i}_sync:
            mov eax, dword ptr [r14 + {snap(B_GRANTS) + 4 * i}]
            mov dword ptr [r14 + {GRANTED + 4 * i}], eax
        gr{i}_end:
            """
        # my picks for the partner: caches bought and guardians slain (hits block +12, +16; the
        # picks I was granted never move these counters, so nothing echoes back). The partner's for
        # me: a pending chest / guardian pick (phases playing, choosing, paused, victory); one still
        # pending while playing is only ever one I granted (the game rolls its own at once), so it
        # opens through the game's roller like a bought chest or a slain guardian
        picks = "" if not full else f"""
            {count('pkc', full['opened'], SEEN_OPENED, 12)}
            {count('pkg', full['slain'], SEEN_SLAIN, 16)}
            {grant(0, full['chests'])}
            {grant(1, full['guards'])}
            cmp byte ptr [r13 + {phase:#x}], 1
            jne pk_end
            cmp byte ptr [r13 + {full['mode']:#x}], 0
            jne pk_end
            mov dl, 1
            cmp dword ptr [r13 + {full['chests']:#x}], 0
            jne pk_roll
            mov dl, 2
            cmp dword ptr [r13 + {full['guards']:#x}], 0
            je pk_end
        pk_roll:
            mov rcx, r13
            mov r8b, 1
            call {full['roller']:#x}
        pk_end:
        """
        start_attack_timer = f"""
            mov eax, dword ptr [r14 + {CLOCK_NOW}]
            mov dword ptr [r14 + {LAST_ATTACK}], eax
        """
        def spell_go(tag="go", target="unit"):
            """SMSG_SPELL_GO of SPELL_ID from the ally: at the unit TARGET (also its one hit), at the
            point EV_POS, or with no target (a self cast)."""
            if target == "unit":
                targets = f"""
            mov rax, qword ptr [r14 + {TARGET}]
            mov qword ptr [rdi], rax
            mov byte ptr [rdi + 8], 0
            mov word ptr [rdi + 9], 2
            add rdi, 11
            mov rdx, rdi
            mov byte ptr [rdi], 0
            inc rdi
            xor ecx, ecx
        {tag}_pack:
            test al, al
            jz {tag}_next
            mov byte ptr [rdi], al
            inc rdi
            mov r8b, 1
            shl r8b, cl
            or byte ptr [rdx], r8b
        {tag}_next:
            shr rax, 8
            inc ecx
            cmp ecx, 8
            jb {tag}_pack
            """
            else:  # no hits, no misses, target mask 0x40 (x, y, z) or 0
                targets = f"""
            mov byte ptr [rdi - 1], 0
            mov byte ptr [rdi], 0
            mov word ptr [rdi + 1], {0x40 if target == "point" else 0}
            add rdi, 3
            """ + (f"""
            mov rax, qword ptr [r14 + {EV_POS}]
            mov qword ptr [rdi], rax
            mov eax, dword ptr [r14 + {EV_POS + 8}]
            mov dword ptr [rdi + 8], eax
            add rdi, 12
            """ if target == "point" else "")
            return f"""
            mov ecx, 64
            mov edx, 1
            call {alloc:#x}
            test rax, rax
            jz {tag}_skip
            mov rbx, rax
            mov rdi, rax
            lea rsi, [r15 + {t_go - block:#x}]
            mov ecx, {len(go_head)}
            rep movsb
            mov eax, dword ptr [r14 + {SPELL_ID}]
            mov dword ptr [rbx + {go_spell}], eax
            {targets}
            {push_built(tag, 0x132)}
        {tag}_skip:
            """

        def push_built(tag, opcode):
            """Queues the packet built in [rbx, rdi) of a 64-byte buffer."""
            return f"""
            mov rsi, rdi
            sub rsi, rbx
            lea rcx, [r13 + {queue:#x}]
            mov rax, qword ptr [rcx + 0x10]
            cmp rax, qword ptr [rcx]
            jne {tag}_room
            call {grow:#x}
            lea rcx, [r13 + {queue:#x}]
            mov rax, qword ptr [rcx + 0x10]
        {tag}_room:
            shl rax, 5
            add rax, qword ptr [rcx + 8]
            mov qword ptr [rax], 64
            mov qword ptr [rax + 8], rbx
            mov qword ptr [rax + 0x10], rsi
            mov qword ptr [rax + 0x18], {opcode:#x}
            inc qword ptr [rcx + 0x10]
            """

        # the ally as my party member (SMSG_GROUP_LIST): the stock party frame and the minimap dot
        # read the live object, so its health/level updates feed them; sent once per spawn, and an
        # empty list when it goes. Type 0, own flags 0, 1 member {nick, guid, online, subgroup 0},
        # leader = my hero, loot method 0, looter 0, threshold 2, one more byte this client reads
        party_join = "" if not ally else f"""
            cmp byte ptr [r14 + {IN_PARTY}], 0
            jne pj_skip
            mov ecx, 64
            mov edx, 1
            call {alloc:#x}
            test rax, rax
            jz pj_skip
            mov rbx, rax
            mov word ptr [rbx], 0
            mov dword ptr [rbx + 2], 1
            lea rdi, [rbx + 6]
            lea rsi, [r14 + {snap(B_NICK)}]
            mov ecx, {NICK_LEN}
        pj_nick:
            mov al, byte ptr [rsi]
            test al, al
            jz pj_named
            mov byte ptr [rdi], al
            inc rsi
            inc rdi
            dec ecx
            jnz pj_nick
        pj_named:
            mov byte ptr [rdi], 0
            mov rax, {ALLY_GUID:#x}
            mov qword ptr [rdi + 1], rax
            mov word ptr [rdi + 9], 1
            mov rax, qword ptr [r13 + {slots['hero_guid']:#x}]
            mov qword ptr [rdi + 11], rax
            mov byte ptr [rdi + 19], 0
            mov qword ptr [rdi + 20], 0
            mov word ptr [rdi + 28], 2
            add rdi, 30
            {push_built('pj', SMSG_GROUP_LIST)}
            mov byte ptr [r14 + {IN_PARTY}], 1
        pj_skip:
        """
        def query(tag, pos, radius):
            """EV_GUID = the live enemy nearest to the point at r14+pos within the radius at consts+radius, or 0;
            a hit (EV_ENTRY) never lands on a boss while the boss sync is live (the query's exclude slice)."""
            exclude = "" if not boss else f"""
            cmp byte ptr [r14 + {EV_ENTRY + 22}], {HIT}
            jne {tag}_all
            cmp byte ptr [r14 + {BOSS_LIVE}], 0
            je {tag}_all
            lea rax, [r14 + {BOSS_GUIDS}]
            mov qword ptr [rsp + 0x20], rax
            mov qword ptr [rsp + 0x28], {BOSS_SLOTS}
        {tag}_all:
            """
            return f"""
            mov qword ptr [rsp + 0x20], 0
            mov qword ptr [rsp + 0x28], 0
            {exclude}
            lea rcx, [rsp + 0x40]
            mov rdx, r13
            lea r8, [r14 + {pos}]
            movss xmm3, dword ptr [r15 + {consts + radius - block:#x}]
            call {enemy_query:#x}
            mov qword ptr [r14 + {EV_GUID}], 0
            mov qword ptr [rsp + 0x58], 0
        {tag}_pick:
            mov rax, qword ptr [rsp + 0x58]
            cmp rax, qword ptr [rsp + 0x50]
            jae {tag}_free
            inc qword ptr [rsp + 0x58]
            mov rdx, qword ptr [rsp + 0x48]
            mov rdx, qword ptr [rdx + rax * 8]
            mov qword ptr [r14 + {EV_GUID}], rdx
            {skip_shared(tag)}
        {tag}_free:
            mov rdx, qword ptr [rsp + 0x40]
            test rdx, rdx
            jz {tag}_done
            mov rcx, qword ptr [rsp + 0x48]
            shl rdx, 3
            mov r8d, 8
            call {dealloc:#x}
        {tag}_done:
            """
        def skip_shared(tag):
            """A shared enemy (while shared) is no replay target: try the next one."""
            return "" if not sh else f"""
            mov rcx, r13
            call {sh['skip']:#x}
            test al, al
            jz {tag}_free
            mov qword ptr [r14 + {EV_GUID}], 0
            jmp {tag}_pick
            """

        def class_spell(tag):
            """eax = the partner's class spell (rdx + rcx * 8: its {spell, school} entry)."""
            return f"""
            mov ecx, dword ptr [r14 + {snap(B_CLASS)}]
            cmp ecx, 11
            jbe {tag}_cls
            xor ecx, ecx
        {tag}_cls:
            lea rdx, [r15 + {spells - block:#x}]
            mov eax, dword ptr [rdx + rcx * 8]
            """

        def secondary_hit(school="xor eax, eax", crit="xor eax, eax"):
            """The game's enemy damage on TARGET for exactly REPLAY_DMG (the hit hook puts it back after
            this game's multiplier), spell SPELL_ID, as a secondary hit (no procs, no spread), never
            captured as mine; school / crit: code leaving them in eax."""
            return f"""
            mov byte ptr [r14 + {REPLAYING}], 1
            mov byte ptr [r14 + {OVERRIDE}], 1
            movzx eax, byte ptr [r13 + {full['kind']:#x}]
            mov byte ptr [r14 + {SAVED_KIND}], al
            mov byte ptr [r13 + {full['kind']:#x}], 3
            {school}
            mov qword ptr [rsp + 0x20], rax
            {crit}
            mov qword ptr [rsp + 0x28], rax
            mov qword ptr [rsp + 0x30], 0
            mov rcx, r13
            mov rdx, qword ptr [r14 + {TARGET}]
            movss xmm2, dword ptr [r14 + {REPLAY_DMG}]
            mov r9d, dword ptr [r14 + {SPELL_ID}]
            call {enemy_damage:#x}
            movzx eax, byte ptr [r14 + {SAVED_KIND}]
            mov byte ptr [r13 + {full['kind']:#x}], al
            mov byte ptr [r14 + {REPLAYING}], 0
            mov byte ptr [r14 + {OVERRIDE}], 0
            """

        # boss sync: the live verdict (playing, the ally shown and fresh, the partner streams boss health),
        # my bosses' keys and fractions for the companion, then the partner's lower fractions dealt
        mine = hits + HITS_BOSS - block if boss else 0
        forget_bosses = "" if not boss else f"""
            xor eax, eax
            lea rdi, [r14 + {BOSS_GUIDS}]
            mov ecx, {BOSS_RESET}
            rep stosb
            lea rdi, [r15 + {mine:#x}]
            mov ecx, {8 * BOSS_SLOTS}
            rep stosb
        """
        bosses = "" if not boss else f"""
            mov byte ptr [r14 + {BOSS_LIVE}], 0
            cmp byte ptr [r13 + {phase:#x}], 1
            jne bs_scan
            cmp dword ptr [r14 + {SPAWNED}], 0
            je bs_scan
            cmp byte ptr [r14 + {ALLY_DEAD}], 0
            jne bs_scan
            cmp dword ptr [r15 + {B_BOSS_ON}], 0
            je bs_scan
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
            subss xmm0, dword ptr [r14 + {LAST_UPD}]
            ucomiss xmm0, dword ptr [r15 + {consts + 4 - block:#x}]
            ja bs_scan
            mov byte ptr [r14 + {BOSS_LIVE}], 1
        bs_scan:
            xor r12d, r12d
            mov rsi, qword ptr [r13 + {full['enemies']:#x}]
            mov rdi, qword ptr [r13 + {full['enemies'] + 8:#x}]
            jmp bs_rec
        bs_next:
            add rsi, {full['size']:#x}
            dec rdi
        bs_rec:
            test rdi, rdi
            jz bs_gone
            cmp byte ptr [rsi + {boss['dead']:#x}], 0
            jne bs_next
            {is_boss('rsi', boss, 'bs_next')}
            mov rax, qword ptr [rsi + {full['guid']:#x}]
            xor ecx, ecx
        bs_find:
            cmp rax, qword ptr [r14 + rcx * 8 + {BOSS_GUIDS}]
            je bs_slot
            inc ecx
            cmp ecx, {BOSS_SLOTS}
            jb bs_find
            xor ecx, ecx
        bs_free:
            cmp dword ptr [r14 + rcx * 4 + {BOSS_KEYS}], 0
            je bs_new
            cmp dword ptr [r15 + rcx * 8 + {mine + 4:#x}], 0
            je bs_new
            inc ecx
            cmp ecx, {BOSS_SLOTS}
            jb bs_free
            jmp bs_next
        bs_new:
            mov qword ptr [r14 + rcx * 8 + {BOSS_GUIDS}], rax
            cmp qword ptr [rsi + {boss['boss']:#x}], -1
            je bs_guard
            inc dword ptr [r14 + {BOSS_COUNT}]
            movzx eax, word ptr [r14 + {BOSS_COUNT}]
            or eax, {BOSS_KEY:#x}
            jmp bs_key
        bs_guard:
            mov rax, qword ptr [rsi + {boss['lair']:#x}]
            and eax, 7
            inc byte ptr [r14 + rax + {BOSS_LIVES}]
            movzx edx, byte ptr [r14 + rax + {BOSS_LIVES}]
            shl eax, 8
            or eax, edx
            or eax, {GUARDIAN_KEY:#x}
        bs_key:
            mov dword ptr [r14 + rcx * 4 + {BOSS_KEYS}], eax
        bs_slot:
            bts r12d, ecx
            mov dword ptr [r14 + rcx * 4 + {BOSS_DMG}], 0
            movss xmm1, dword ptr [rsi + {boss['max']:#x}]
            xorps xmm0, xmm0
            ucomiss xmm1, xmm0
            jbe bs_next
            movss xmm0, dword ptr [rsi + {full['hp']:#x}]
            divss xmm0, xmm1
            maxss xmm0, dword ptr [r15 + {consts + 60 - block:#x}]
            minss xmm0, dword ptr [r15 + {consts + 56 - block:#x}]
            movd edx, xmm0
            shl rdx, 32
            mov eax, dword ptr [r14 + rcx * 4 + {BOSS_KEYS}]
            or rax, rdx
            mov qword ptr [r15 + rcx * 8 + {mine:#x}], rax
            cmp byte ptr [r14 + {BOSS_LIVE}], 0
            je bs_next
            mov r8d, dword ptr [r15 + {B_BOSS_N}]
            cmp r8d, {BOSS_SLOTS}
            jbe bs_partner
            mov r8d, {BOSS_SLOTS}
        bs_partner:
            test r8d, r8d
            jz bs_next
            dec r8d
            mov rdx, qword ptr [r15 + r8 * 8 + {B_BOSS}]
            cmp edx, eax
            jne bs_partner
            shr rdx, 32
            jz bs_kill
            movd xmm1, edx
            movaps xmm2, xmm0
            subss xmm2, xmm1
            ucomiss xmm2, dword ptr [r15 + {consts + 52 - block:#x}]
            jbe bs_next
            mulss xmm1, dword ptr [rsi + {boss['max']:#x}]
            movss xmm0, dword ptr [rsi + {full['hp']:#x}]
            subss xmm0, xmm1
            jmp bs_damage
        bs_kill:
            movss xmm0, dword ptr [rsi + {full['hp']:#x}]
            addss xmm0, dword ptr [rsi + {boss['max']:#x}]
        bs_damage:
            movss dword ptr [r14 + rcx * 4 + {BOSS_DMG}], xmm0
            jmp bs_next
        bs_gone:
            xor ecx, ecx
        bs_gone_next:
            bt r12d, ecx
            jc bs_gone_skip
            mov eax, dword ptr [r14 + rcx * 4 + {BOSS_KEYS}]
            mov qword ptr [r15 + rcx * 8 + {mine:#x}], rax
            mov dword ptr [r14 + rcx * 4 + {BOSS_DMG}], 0
        bs_gone_skip:
            inc ecx
            cmp ecx, {BOSS_SLOTS}
            jb bs_gone_next
            cmp byte ptr [r14 + {BOSS_LIVE}], 0
            je bs_end
            {class_spell('bs')}
            mov dword ptr [r14 + {SPELL_ID}], eax
            xor r12d, r12d
        bs_deal:
            movss xmm0, dword ptr [r14 + r12 * 4 + {BOSS_DMG}]
            xorps xmm1, xmm1
            ucomiss xmm0, xmm1
            jbe bs_dealt
            mov dword ptr [r14 + r12 * 4 + {BOSS_DMG}], 0
            movss dword ptr [r14 + {REPLAY_DMG}], xmm0
            mov rax, qword ptr [r14 + r12 * 8 + {BOSS_GUIDS}]
            mov qword ptr [r14 + {TARGET}], rax
            {secondary_hit()}
        bs_dealt:
            inc r12d
            cmp r12d, {BOSS_SLOTS}
            jb bs_deal
        bs_end:
        """
        # the partner's real hits and casts (ring in the block, flags bit3): up to EV_PER_TICK a tick.
        # A hit lands on the live enemy nearest to the partner's target point (else nearest to the
        # ally) through the game's own enemy damage with the partner's final amount (the hit hook puts
        # it back after this game's multiplier), as a secondary hit (no procs, no spread) and never
        # captured as mine; a cast is the partner's SPELL_GO from the ally
        replay = "" if not full else f"""
            test byte ptr [r14 + {snap(B_FLAGS)}], {FLAG_EVENTS}
            jz dps_mode
            cmp byte ptr [r13 + {phase:#x}], 1
            jne atk_end
            mov byte ptr [r14 + {APPLIED}], 1
            {class_spell('rp')}
            mov dword ptr [r14 + {CLASS_SPELL}], eax
            mov dword ptr [r14 + {EV_N}], 0
        rp_next:
            mov eax, dword ptr [r15 + {B_EV_TAIL}]
            cmp eax, dword ptr [r15 + {B_EV_HEAD}]
            je atk_end
            cmp dword ptr [r14 + {EV_N}], {EV_PER_TICK}
            jae atk_end
            inc dword ptr [r14 + {EV_N}]
            and eax, {EV_RING - 1}
            imul eax, eax, {EV_SIZE}
            lea rsi, [r15 + rax + {B_EV}]
            lea rdi, [r14 + {EV_ENTRY}]
            mov ecx, {EV_SIZE}
            rep movsb
            inc dword ptr [r15 + {B_EV_TAIL}]
            mov rax, qword ptr [r14 + {EV_ENTRY + 8}]
            mov qword ptr [r14 + {EV_POS}], rax
            mov eax, dword ptr [r14 + {snap(B_POS) + 8}]
            mov dword ptr [r14 + {EV_POS + 8}], eax
            mov eax, dword ptr [r14 + {EV_ENTRY}]
            test eax, eax
            jnz rp_spell
            mov eax, dword ptr [r14 + {CLASS_SPELL}]
        rp_spell:
            mov dword ptr [r14 + {SPELL_ID}], eax
            movzx eax, byte ptr [r14 + {EV_ENTRY + 22}]
            cmp eax, {CAST_POINT}
            je rp_point
            cmp eax, {CAST_SELF}
            je rp_self
            {query('rq1', EV_POS, 44)}
            cmp qword ptr [r14 + {EV_GUID}], 0
            jne rp_found
            {query('rq2', snap(B_POS), 32)}
            cmp qword ptr [r14 + {EV_GUID}], 0
            je rp_next
        rp_found:
            mov rax, qword ptr [r14 + {EV_GUID}]
            mov qword ptr [r14 + {TARGET}], rax
            cmp byte ptr [r14 + {EV_ENTRY + 22}], {HIT}
            jne rp_unit
            mov eax, dword ptr [r14 + {EV_ENTRY + 4}]
            mov dword ptr [r14 + {REPLAY_DMG}], eax
            {secondary_hit(f"movzx eax, byte ptr [r14 + {EV_ENTRY + 16}]",
                           f"movzx eax, byte ptr [r14 + {EV_ENTRY + 20}]" + chr(10) + "and eax, 1")}
            jmp rp_next
        rp_unit:
            {spell_go('rgu', 'unit')}
            jmp rp_next
        rp_point:
            {spell_go('rgp', 'point')}
            jmp rp_next
        rp_self:
            {spell_go('rgs', 'self')}
            jmp rp_next
        dps_mode:
        """
        attack = "" if not ally else f"""
            {replay}
            cmp byte ptr [r13 + {phase:#x}], 1
            jne atk_end
            test byte ptr [r14 + {snap(B_FLAGS)}], {FLAG_DEAD | FLAG_PAUSED}
            jnz atk_end
            movss xmm0, dword ptr [r14 + {snap(B_DPS)}]
            xorps xmm1, xmm1
            ucomiss xmm0, xmm1
            jbe atk_end
            minss xmm0, dword ptr [r15 + {consts + 36 - block:#x}]
            movss xmm1, dword ptr [r14 + {CLOCK_NOW}]
            subss xmm1, dword ptr [r14 + {LAST_ATTACK}]
            ucomiss xmm1, dword ptr [r15 + {consts + 24 - block:#x}]
            jb atk_end
            minss xmm1, dword ptr [r15 + {consts + 28 - block:#x}]
            mulss xmm0, xmm1
            movss dword ptr [r14 + {DMG_EACH}], xmm0
            mov eax, dword ptr [r14 + {CLOCK_NOW}]
            mov dword ptr [r14 + {LAST_ATTACK}], eax
            {class_spell('atk')}
            mov dword ptr [r14 + {SPELL_ID}], eax
            mov eax, dword ptr [rdx + rcx * 8 + 4]
            mov dword ptr [r14 + {SCHOOL}], eax
            mov eax, dword ptr [r14 + {snap(B_SPELL)}]
            test eax, eax
            jz spell_ok
            mov dword ptr [r14 + {SPELL_ID}], eax
        spell_ok:
            lea rcx, [rsp + 0x40]
            mov rdx, r13
            lea r8, [r14 + {snap(B_POS)}]
            movss xmm3, dword ptr [r15 + {consts + 32 - block:#x}]
            mov qword ptr [rsp + 0x20], {QUERY_LIMIT}
            mov qword ptr [rsp + 0x28], 0
            call {enemy_query:#x}
            mov rax, qword ptr [rsp + 0x50]
            test rax, rax
            jz atk_free
            mov ecx, {MAX_TARGETS}
            cmp rax, rcx
            cmova rax, rcx
            mov dword ptr [r14 + {N_TARGETS}], eax
            cvtsi2ss xmm1, eax
            movss xmm0, dword ptr [r14 + {DMG_EACH}]
            divss xmm0, xmm1
            movss dword ptr [r14 + {DMG_EACH}], xmm0
            xor r12d, r12d
        atk_loop:
            cmp r12d, dword ptr [r14 + {N_TARGETS}]
            jae atk_free
            mov rax, qword ptr [rsp + 0x48]
            mov rax, qword ptr [rax + r12 * 8]
            mov qword ptr [r14 + {TARGET}], rax
            {spell_go()}
            mov rcx, r13
            mov rdx, qword ptr [r14 + {TARGET}]
            movss xmm2, dword ptr [r14 + {DMG_EACH}]
            mov r9d, dword ptr [r14 + {SPELL_ID}]
            mov eax, dword ptr [r14 + {SCHOOL}]
            mov qword ptr [rsp + 0x20], rax
            mov qword ptr [rsp + 0x28], 0
            mov qword ptr [rsp + 0x30], 0
            mov byte ptr [r14 + {REPLAYING}], 1
            call {enemy_damage:#x}
            mov byte ptr [r14 + {REPLAYING}], 0
            inc r12d
            jmp atk_loop
        atk_free:
            mov rdx, qword ptr [rsp + 0x40]
            test rdx, rdx
            jz atk_end
            mov rcx, qword ptr [rsp + 0x48]
            shl rdx, 3
            mov r8d, 8
            call {dealloc:#x}
        atk_end:
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
        """
        payload = PAYLOAD[1] - PAYLOAD[0]
        # run sync: the companion's clock correction, once per adjust_seq, while playing; the clock
        # this tick read is moved along so the new-run check above doesn't see a jump back as a new run
        adjust = "" if not run_sync else f"""
            test byte ptr [r14 + {snap(B_CONTROL)}], {COOP_LIVE}
            jz adj_end
            cmp byte ptr [r13 + {phase:#x}], 1
            jne adj_end
            mov eax, dword ptr [r14 + {snap(B_ADJ_SEQ)}]
            cmp eax, dword ptr [r14 + {ADJ_SEEN}]
            je adj_end
            mov dword ptr [r14 + {ADJ_SEEN}], eax
            movss xmm0, dword ptr [r13 + {clock:#x}]
            addss xmm0, dword ptr [r14 + {snap(B_ADJ)}]
            xorps xmm1, xmm1
            ucomiss xmm0, xmm1
            jb adj_end
            movss dword ptr [r13 + {clock:#x}], xmm0
            movss dword ptr [r14 + {CLOCK_NOW}], xmm0
            movss dword ptr [r14 + {LAST_CLOCK}], xmm0
        adj_end:
            cmp byte ptr [r14 + {HELD}], 0
            je note_end
            mov byte ptr [r14 + {HELD}], 0
            cmp byte ptr [r13 + {phase:#x}], 0
            jne note_end
            movss xmm0, dword ptr [r14 + {REAL_T}]
            subss xmm0, dword ptr [r14 + {NOTE_AT}]
            ucomiss xmm0, dword ptr [r15 + {consts + 48 - block:#x}]
            jb note_end
            mov eax, dword ptr [r14 + {REAL_T}]
            mov dword ptr [r14 + {NOTE_AT}], eax
            {announce_call(1, 'wait', False)}
        note_end:
        """
        code = f"""
            push r15
            lea r15, [rip + {block - (va + 9):#x}]
            push rbx
            push rsi
            push rdi
            push r12
            push r13
            push r14
            sub rsp, 0x60
            mov r13, rcx
            lea r14, [r15 + {state - block:#x}]
            mov qword ptr [r14 + {STATE_PTR}], r13
            mov eax, dword ptr [r13 + {clock:#x}]
            mov dword ptr [r14 + {CLOCK_NOW}], eax
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
            ucomiss xmm0, dword ptr [r14 + {LAST_CLOCK}]
            mov dword ptr [r14 + {LAST_CLOCK}], eax
            jae same_run
            mov dword ptr [r14 + {LAST_UPD}], {LONG_AGO:#x}
            mov byte ptr [r14 + {NEW}], 0
            mov byte ptr [r14 + {DEFEATED}], 0
            mov byte ptr [r14 + {ANNOUNCED}], 0
            mov byte ptr [r14 + {ALLY_DEAD}], 0
            {forget_bosses}
            cmp dword ptr [r14 + {SPAWNED}], 0
            je same_run
            {send('rst', t_destroy, len(destroy), 0xAA, ok=gone)}
            {drop_summons}
            {party_leave.replace('TAG', '0')}
        same_run:
            mov eax, dword ptr [r15 + {B_SEQ}]
            test al, 1
            jnz evaluate
            lea rsi, [r15 + {PAYLOAD[0]}]
            lea rdi, [r14 + {TMP}]
            mov ecx, {payload}
            rep movsb
            cmp eax, dword ptr [r15 + {B_SEQ}]
            jne evaluate
            lea rsi, [r14 + {TMP}]
            lea rdi, [r14 + {SNAP}]
            mov ecx, {payload}
            rep movsb
            cmp eax, dword ptr [r14 + {LAST_SEQ}]
            je evaluate
            mov dword ptr [r14 + {LAST_SEQ}], eax
            mov eax, dword ptr [r14 + {CLOCK_NOW}]
            mov dword ptr [r14 + {LAST_UPD}], eax
            mov byte ptr [r14 + {NEW}], 1
        evaluate:
            {adjust}
            {picks}
            {bosses}
            {shared_tick}
            {defeat}
        alive:
            movss xmm0, dword ptr [r14 + {CLOCK_NOW}]
            movaps xmm1, xmm0
            subss xmm1, dword ptr [r14 + {LAST_UPD}]
            cmp dword ptr [r14 + {SPAWNED}], 0
            je not_spawned
            cmp dword ptr [r14 + {snap(B_ACTIVE)}], 0
            je despawn
            ucomiss xmm1, dword ptr [r15 + {consts + 4 - block:#x}]
            ja despawn
            cmp byte ptr [r14 + {ALLY_DEAD}], 0
            jne sum_tail
            {health_sync}
            {party_join}
            {attack}
            cmp byte ptr [r14 + {NEW}], 0
            je sum_tail
            cmp byte ptr [r13 + {phase:#x}], 1
            jne sum_tail
            subss xmm0, dword ptr [r14 + {LAST_MOVE}]
            ucomiss xmm0, dword ptr [r15 + {consts - block:#x}]
            jb sum_tail
            mulss xmm0, dword ptr [r15 + {consts + 8 - block:#x}]
            maxss xmm0, dword ptr [r15 + {consts + 12 - block:#x}]
            minss xmm0, dword ptr [r15 + {consts + 16 - block:#x}]
            cvttss2si r12d, xmm0
            {send('mov', t_move, len(move), 0xDD, move_fill, ok=moved + keep_pos)}
        sum_tail:
            {sync}
            jmp done
        despawn:
            {send('des', t_destroy, len(destroy), 0xAA, ok=gone)}
            {drop_summons}
            {party_leave.replace('TAG', '1')}
            jmp done
        not_spawned:
            cmp dword ptr [r14 + {snap(B_ACTIVE)}], 0
            je done
            {wait_for_nick}
            cmp byte ptr [r13 + {phase:#x}], 1
            jne done
            ucomiss xmm1, dword ptr [r15 + {consts + 4 - block:#x}]
            ja done
            {send('new', t_create, len(create), 0xA9, create_fill,
                  ok=f"mov dword ptr [r14 + {SPAWNED}], 1" + keep_pos
                  + start_attack_timer + f"mov byte ptr [r14 + {ALLY_DEAD}], 0"
                  + f"\n mov dword ptr [r14 + {LAST_HPM}], 0")}
        done:
            cmp byte ptr [r14 + {APPLIED}], 0
            jne ev_kept
            mov eax, dword ptr [r15 + {B_EV_HEAD}]
            mov dword ptr [r15 + {B_EV_TAIL}], eax
        ev_kept:
            mov byte ptr [r14 + {APPLIED}], 0
            add rsp, 0x60
            pop r14
            pop r13
            pop r12
            pop rdi
            pop rsi
            pop rbx
            pop r15
            ret
        {sum_sync}
        """
        return p.asm(code, va)

    ghost_tick = p.add_cave(build_tick)
    site = p.va(tick.start(1))
    resume = p.va(tick.end(1))

    # at the hook rsp is 16-aligned (frame fully set up); keep every volatile register
    def build_hook(va):
        return p.asm(f"""
            mov r13, rcx
            push rax
            push rcx
            push rdx
            push r8
            push r9
            push r10
            push r11
            sub rsp, 0x68
            movups xmmword ptr [rsp + 0x08], xmm0
            movups xmmword ptr [rsp + 0x18], xmm1
            movups xmmword ptr [rsp + 0x28], xmm2
            movups xmmword ptr [rsp + 0x38], xmm3
            movups xmmword ptr [rsp + 0x48], xmm4
            movups xmmword ptr [rsp + 0x58], xmm5
            call {ghost_tick:#x}
            movups xmm0, xmmword ptr [rsp + 0x08]
            movups xmm1, xmmword ptr [rsp + 0x18]
            movups xmm2, xmmword ptr [rsp + 0x28]
            movups xmm3, xmmword ptr [rsp + 0x38]
            movups xmm4, xmmword ptr [rsp + 0x48]
            movups xmm5, xmmword ptr [rsp + 0x58]
            add rsp, 0x68
            pop r11
            pop r10
            pop r9
            pop r8
            pop rdx
            pop rcx
            pop rax
            movzx eax, byte ptr [r13 + {phase:#x}]
            jmp {resume:#x}
        """, va)

    hook = p.add_cave(build_hook)
    p.write(site, p.asm(f"jmp {hook:#x}", site).ljust(len(tick.group(1)), b"\x90"), expect=tick.group(1))
    choice_g = None
    # Install optional hooks atomically: failed signatures/assembly restore the
    # complete pre-choice patch state, then install the established co-op hooks.
    saved = (p.d[:], p.cave[:], p.data[:], p.relocs[:], p.meta.copy(), p.log[:])
    if full and run_sync and hero_damage and announce:
        try:
            choice.prepare(p, hero_damage, enemy_damage, run_sync['roller'])
            choice_g = choice.allocate(p, block=block, state=state, phase=phase, clock=clock,
                                      hero_damage=hero_damage, enemy_damage=enemy_damage, full=full,
                                      queue=queue, grow=grow, announce=announce, B_LANG=B_LANG,
                                      hero_guid=slots['hero_guid'], dealloc=dealloc,
                                      B_CONTROL=B_CONTROL, COOP_LIVE=COOP_LIVE, REAL_T=REAL_T,
                                      FAKED=FAKED, REPLAYING=REPLAYING, DEFEATED=DEFEATED,
                                      DEFEAT_HIT=DEFEAT_HIT, STATE_PTR=STATE_PTR)
            if sh:
                choice_g['hold'] = choice.install_hold(p, choice_g, sh)
                choice_g['hero'] = boss['hero']
                choice_g['peer_position'] = state + snap(B_POS)
                choice_g['push'] = choice_push.install(p, choice_g, sh)
            choice_g['cast_filter'] = choice_cast.install(p, choice_g, slots, dealloc)
            choice.hook_damage(p, choice_g)
            hook_hits(p, full, slots, state, hits, clock, boss, sh, choice_g)
            if boss:
                hook_boss_chase(p, boss, full, state, sh, choice_g)
            hook_run_sync(p, run_sync, block, state, phase, clock, choice_g)
            p.meta['choice_update_hook'] = choice_g['update'] - p.ib
            p.log.append(f"coop: choice shield enabled, block {choice_g['choice']:x}")
        except Exception as e:
            if type(e).__name__ not in ('PatchError', 'KsError'):
                raise
            p.d, p.cave, p.data, p.relocs, p.meta, p.log = saved
            choice_g = None
            p.log.append(f"coop: choice shield disabled ({e})")
    if not choice_g:
        if full:
            hook_hits(p, full, slots, state, hits, clock, boss, sh)
        if boss:
            hook_boss_chase(p, boss, full, state, sh)
        if run_sync:
            hook_run_sync(p, run_sync, block, state, phase, clock)
    if stats:
        hook_damage_stats(p, stats, state)

    # name query for the ally guid: the partner's nick, race, gender and class (non-empty nick only)
    for m in name_sites:
        replay = m.group(1)
        back = p.va(m.end(1))

        def build_name(va, replay=replay, back=back):
            body = p.asm(f"""
                lea r11, [rip + {state + snap(B_NICK) - (va + 7):#x}]
                mov rax, {ALLY_GUID:#x}
                cmp rdx, rax
                jne keep
                xor eax, eax
            count:
                cmp eax, {NICK_LEN}
                jae counted
                cmp byte ptr [r11 + rax], 0
                je counted
                inc eax
                jmp count
            counted:
                test eax, eax
                jz keep
                mov r8, r11
                mov r9d, eax
                mov al, byte ptr [r11 + {B_RACE - B_NICK}]
                mov byte ptr [rsp + 0x20], al
                mov al, byte ptr [r11 + {B_GENDER - B_NICK}]
                mov byte ptr [rsp + 0x28], al
                mov al, byte ptr [r11 + {B_CLASS - B_NICK}]
                mov byte ptr [rsp + 0x30], al
            keep:
                nop
            """, va)
            return body + replay + p.asm(f"jmp {back:#x}", va + len(body) + len(replay))

        cave = p.add_cave(build_name)
        site_va = p.va(m.start(1))
        p.write(site_va, p.asm(f"jmp {cave:#x}", site_va).ljust(len(replay), b"\x90"), expect=replay)
