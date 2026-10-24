"""Quests during a run: a quest giver appears, walking up to it starts a timed errand, its
reward is the game's own item pick (as a chest gives). Logic and UI are Lua (quest.lua); the
exe only gets a small bridge.

- Lua: quest.lua is appended to the game's built-in azs_api.lua chunk (the one that turns the
  server's addon lines into AZS_RUN, AZS_CHESTS, ..). It goes in through loadstring, so a
  syntax error in it can't take the API down with it.
- Lua -> exe: the cvar "azsQuest". Lua sets it to one command; a hook at the entry of the
  client system that reads the storeLevels/spendLevels cvars registers it, copies a command
  into a .moddata mailbox and sets the cvar back to "0", which tells Lua the next may come.
- exe -> world: a hook in the run tick, where the clock advances (phase `playing` only), runs
  the mailbox's command and empties it. The cvar store keeps numbers only, so a command is
  digits: the op, then each argument as FIELD digits holding value + BIAS.
    1 slot x y z facing display npcflags scale  create creature `slot` (client-side only, like
                                                coop_ghost's), x/y/z in 1/10 yd, facing in
                                                1/1000 rad, scale in 1/100 (0 = 1.0)
    2 slot                                      destroy it
    3 slot status                               SMSG_QUESTGIVER_STATUS (the "!" overhead)
    4                                           one more chest item pick, as opening a chest
"""
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coop_ghost as coop  # noqa: E402  packet helpers and signatures shared with the co-op ghost

LUA = Path(__file__).with_suffix(".lua")
CVAR = b"azsQuest"
SLOTS = 6  # 0 the quest giver, 1..5 the waypoint markers
QUEST_GUID = 0xF130_0023_29FF_FFC0  # + slot, creature guids below the ghost's and summons'
FIELD_SCALE, FIELD_NPC_FLAGS = 0x04, 0x93
SMSG_UPDATE_OBJECT, SMSG_DESTROY_OBJECT, SMSG_QUESTGIVER_STATUS = 0xA9, 0xAA, 0x183
ROLL_CHEST = 1

# block layout
MAIL, REG, BUF, ARGS, CONSTS, NAME, ZERO = 8, 12, 16, 80, 112, 128, 144
BUF_MAX, NARGS = 63, 8
# a command is digits only (the cvar store drops values that aren't numbers): an op digit, then
# each argument as FIELD digits holding value + BIAS
FIELD, BIAS = 7, 5_000_000
OP_SPAWN, OP_DESTROY, OP_STATUS, OP_REWARD = (ord(c) for c in "1234")
TEMPLATES = 160

# the azs_api.lua chunk is handed to str::replace: lea rdx, [chunk] ; lea r9, ["HUD_CVARS_LUA"] ; mov r8d, len
LUA_SITE = re.compile(rb"\x48\x8d\x15(....)\x4c\x8d\x0d(....)\x41\xb8(....)", re.S)
LUA_HEAD = b"\n\n-- Azeroth Survivors: the run for addons"
# run tick (coop_ghost hooks its entry, r13 = run state): cmp al, 1 (phase playing) ; .. ; jne ;
# movss xmm7, [r13+clock] ; addss xmm7, xmm6
PLAYING_CLOCK = re.compile(rb"\x3c\x01.{0,24}?\x0f\x85....(" + coop.CLOCK.pattern + rb")", re.S)
# the cvar system: register / read / set of "spendLevels" (rcx or rdx = the cvar store in rsi)
CVAR_NAMES = b"storeLevelsspendLevels"
CVAR_REG = re.compile(rb"\x48\xc7\x44\x24\x20\x01\x00\x00\x00\x48\x8d\x15(....)\x41\xb8\x0b\x00\x00\x00"
                      rb"\x48\x89\xf1[\x49\x4c]\x89.\xe8(....)", re.S)
# lea rcx, [rbp/rsp + slot] (the out Option<String>) in any of its encodings
CVAR_READ = re.compile(rb"\x4c\x8d\x05(....)\x48\x8d(?:\x4d.|\x8d....|\x4c\x24.|\x8c\x24....)\x41\xb9\x0b\x00\x00\x00\x48\x89\xf2\xe8(....)", re.S)
CVAR_SET = re.compile(rb"\x48\xc7\x44\x24\x20\x01\x00\x00\x00\x48\x8d\x15(....)\x4c\x8d\x0d....\x41\xb8\x0b\x00\x00\x00"
                      rb"\x48\x89\xf1\xe8(....)", re.S)
# a chest's item pick is coop.CHEST_ROLL (state in rcx): phase, pending (chest picks still owed;
# opening a chest adds one, closing a pick takes one and rolls the next while any are left),
# frozen, roller(state, kind, 1)


def s32(b):
    return struct.unpack("<i", b)[0]


def rel(p, m, g, *, record=False):
    """Target of a rip-relative operand or rel32 that ends its instruction."""
    return p.offsets.resolve_capture(m.start(g), len(m.group(g)), record=record)


def one(p, pattern, what, lo=0, hi=None, pred=lambda m: True):
    hits = [m for m in pattern.finditer(p.d, lo, hi or len(p.d)) if pred(m)]
    p.check(len(hits) == 1, f"quest: {what}: {len(hits)} hits instead of 1")
    return hits[0]


def lua_source():
    src = LUA.read_text(encoding="utf-8")
    if "[[" in src or "]]" in src:
        raise AssertionError("quest.lua must not contain [[ or ]] (it is wrapped in a Lua 5.0 long string)")
    wrap = ("\n-- Azeroth Survivors mod: quests (mod/patches/quest.lua)\n"
            "local f, e = loadstring([[\n" + src + "\n]], \"azs_quest\")\n"
            "if ( f ) then f(); else AZQ_LOAD_ERROR = e; end\n")
    return wrap.encode("utf-8")


def cvar_prologue(p, system):
    """The cvar system's pushes and `sub rsp, N` (replayed by the hook, whatever N is), checked to
    keep the cvar store from rdx in rsi, where the register/read/set calls take it from."""
    insns = p.insns(p.off(system), 16)
    n = next((k for k, i in enumerate(insns) if i.mnemonic != "push"), 0)
    p.check(0 < n and insns[n].mnemonic == "sub" and insns[n].op_str.startswith("rsp, "),
            f"quest: cvar system {system:x} has an unknown prologue")
    p.check(any(i.mnemonic == "mov" and i.op_str == "rsi, rdx" for i in insns[n + 1:]),
            "quest: the cvar system no longer keeps the store in rsi")
    prologue = bytes(p.d[p.off(system):p.off(insns[n].address) + insns[n].size])
    p.check(len(prologue) >= 5, "quest: cvar system prologue too short for a jmp")
    return prologue


def patch_lua(p):
    m = one(p, LUA_SITE, "azs_api.lua chunk",
            pred=lambda m: p.d.startswith(LUA_HEAD, p.off(rel(p, m, 1)))
            and p.d.startswith(b"HUD_CVARS_LUA", p.off(rel(p, m, 2))))
    chunk_va, size = rel(p, m, 1, record=True), struct.unpack("<I", m.group(3))[0]
    chunk = bytes(p.d[p.off(chunk_va):p.off(chunk_va) + size])
    p.check(b"AZSApiFrame:SetScript" in chunk[-200:], "quest: azs_api.lua chunk does not end as expected")
    new = chunk + lua_source()
    va = p.add_data(len(new), new)
    lea = p.va(m.start())
    p.write(lea, p.asm(f"lea rdx, [rip + {va - (lea + 7):#x}]", 0)[:7], expect=bytes(p.d[p.off(lea):p.off(lea) + 7]))
    p.write(p.va(m.start(3)), struct.pack("<I", len(new)), expect=m.group(3))
    p.log.append(f"quest: azs_api.lua {size} -> {len(new)} bytes")


def patch(p):
    patch_lua(p)
    queue = coop.find_queue(p)
    alloc, grow, dealloc = coop.find_alloc(p), coop.find_grow(p, queue), coop.find_dealloc(p)

    # run tick: where the clock advances, right behind its `phase == playing` branch
    clock = one(p, PLAYING_CLOCK, "run clock")
    clock_off = s32(clock.group(2))
    # the state fields the reward uses, read from the chest pick and checked against the guardian
    # pick (same fields and roller, on the run tick's r13) and the chest counter's increment
    roll = one(p, coop.CHEST_ROLL, "chest item pick")
    phase, pending, frozen, roller = s32(roll.group(1)), s32(roll.group(2)), s32(roll.group(3)), rel(p, roll, 4)
    guard = one(p, coop.GUARD_ROLL, "guardian item pick")
    p.check(coop.func_start(p, p.va(guard.start())) == coop.func_start(p, p.va(clock.start(1))),
            "quest: guardian pick is not in the run tick")
    p.check(phase == s32(guard.group(3)) and frozen == s32(guard.group(2)) and rel(p, guard, 4) == roller,
            "quest: chest and guardian picks disagree")
    one(p, re.compile(coop.OPENED + re.escape(roll.group(2)), re.S), "chest counter increment")

    # client cvar system
    names = p.d.find(CVAR_NAMES)
    p.check(names > 0 and p.d.find(CVAR_NAMES, names + 1) < 0, "quest: storeLevels/spendLevels names")
    spend = p.va(names) + 11
    reg = [m for m in CVAR_REG.finditer(p.d) if rel(p, m, 1) == spend]
    read = [m for m in CVAR_READ.finditer(p.d) if rel(p, m, 1) == spend]
    put = [m for m in CVAR_SET.finditer(p.d) if rel(p, m, 1) == spend]
    p.check(len(reg) == len(read) == len(put) == 1, f"quest: spendLevels register/read/set {len(reg)}/{len(read)}/{len(put)}")
    system = coop.func_start(p, p.va(read[0].start()))
    p.check(system is not None and all(coop.func_start(p, p.va(m[0].start())) == system for m in (reg, put)),
            "quest: spendLevels register/read/set are not in one function")
    reg, read, put = rel(p, reg[0], 2), rel(p, read[0], 2), rel(p, put[0], 2)
    prologue = cvar_prologue(p, system)

    # per-slot packet templates, back to back
    creates = [coop.create_packet(False, QUEST_GUID + i, {FIELD_NPC_FLAGS: 0}) for i in range(SLOTS)]
    c_size, c_off = len(creates[0][0]), creates[0][1]
    p.check(all(len(c) == c_size for c, _ in creates), "quest: create templates differ in size")
    t_create = b"".join(c for c, _ in creates)
    t_destroy = b"".join((QUEST_GUID + i).to_bytes(8, "little") for i in range(SLOTS))
    t_status = b"".join((QUEST_GUID + i).to_bytes(8, "little") + b"\0\0\0\0" for i in range(SLOTS))
    head = bytearray(TEMPLATES)
    head[:8] = b"AZSQUEST"
    struct.pack_into("<3f", head, CONSTS, 10.0, 1000.0, 100.0)
    head[NAME:NAME + len(CVAR) + 1] = CVAR + b"\0"
    head[ZERO:ZERO + 2] = b"0\0"
    T_CREATE, T_DESTROY = TEMPLATES, TEMPLATES + len(t_create)
    T_STATUS = T_DESTROY + len(t_destroy)
    body = bytes(head) + t_create + t_destroy + t_status
    block = p.add_data(len(body), body)
    pos, disp, native = c_off["pos"], c_off["display"], c_off["native"]
    npcf, scale = c_off["at"](FIELD_NPC_FLAGS), c_off["at"](FIELD_SCALE)

    def push(tag, size, opcode):
        """Pushes the packet buffer in rbx (size bytes) to the outbound queue."""
        return f"""
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
        """

    def copy(size, table, stride):
        """rbx = a new buffer holding template `table` + slot * stride; jumps to finish if out of memory."""
        return f"""
            mov ecx, {size}
            mov edx, 1
            call {alloc:#x}
            test rax, rax
            jz finish
            mov rbx, rax
            mov rdi, rax
            mov eax, dword ptr [r15 + {ARGS}]
            imul eax, eax, {stride}
            lea rsi, [r15 + rax + {table}]
            mov ecx, {size}
            rep movsb
        """

    def build_tick(va):
        coord = lambda arg, at, k: f"""
            cvtsi2ss xmm0, dword ptr [r15 + {ARGS + 4 * arg}]
            divss xmm0, dword ptr [r15 + {CONSTS + 4 * k}]
            movss dword ptr [rbx + {at}], xmm0
        """
        return p.asm(f"""
            lea rax, [rip + {block - (va + 7):#x}]
            push rbx
            push rsi
            push rdi
            push r15
            sub rsp, 0x28
            mov r15, rax
            cmp dword ptr [r15 + {MAIL}], 0
            je done
            lea rsi, [r15 + {BUF + 1}]
            xor ecx, ecx
        zero:
            mov dword ptr [r15 + rcx * 4 + {ARGS}], 0
            inc ecx
            cmp ecx, {NARGS}
            jb zero
            xor ecx, ecx
        next_arg:
            cmp ecx, {NARGS}
            jae parsed
            xor edx, edx
            mov r8d, {FIELD}
        digits:
            movzx eax, byte ptr [rsi]
            sub eax, 0x30
            cmp eax, 9
            ja parsed
            imul edx, edx, 10
            add edx, eax
            inc rsi
            dec r8d
            jnz digits
            sub edx, {BIAS}
            mov dword ptr [r15 + rcx * 4 + {ARGS}], edx
            inc ecx
            jmp next_arg
        parsed:
            movzx eax, byte ptr [r15 + {BUF}]
            cmp al, {OP_REWARD}
            je reward
            cmp dword ptr [r15 + {ARGS}], {SLOTS}
            jae finish
            cmp al, {OP_SPAWN}
            je spawn
            cmp al, {OP_DESTROY}
            je destroy
            cmp al, {OP_STATUS}
            je status
            jmp finish

        reward:
            inc dword ptr [r13 + {pending:#x}]
            cmp byte ptr [r13 + {phase:#x}], 1
            jne finish
            cmp byte ptr [r13 + {frozen:#x}], 0
            jne finish
            mov rcx, r13
            mov dl, {ROLL_CHEST}
            mov r8b, 1
            call {roller:#x}
            jmp finish

        spawn:
            {copy(c_size, T_CREATE, c_size)}
            {coord(1, pos, 0)}
            {coord(2, pos + 4, 0)}
            {coord(3, pos + 8, 0)}
            {coord(4, pos + 12, 1)}
            mov eax, dword ptr [r15 + {ARGS + 20}]
            mov dword ptr [rbx + {disp}], eax
            mov dword ptr [rbx + {native}], eax
            mov eax, dword ptr [r15 + {ARGS + 24}]
            mov dword ptr [rbx + {npcf}], eax
            cmp dword ptr [r15 + {ARGS + 28}], 0
            je spawn_push
            {coord(7, scale, 2)}
        spawn_push:
            {push("spawn", c_size, SMSG_UPDATE_OBJECT)}
            jmp finish

        destroy:
            {copy(8, T_DESTROY, 8)}
            {push("destroy", 8, SMSG_DESTROY_OBJECT)}
            jmp finish

        status:
            {copy(12, T_STATUS, 12)}
            mov eax, dword ptr [r15 + {ARGS + 4}]
            mov dword ptr [rbx + 8], eax
            {push("status", 12, SMSG_QUESTGIVER_STATUS)}

        finish:
            mov dword ptr [r15 + {MAIL}], 0
        done:
            add rsp, 0x28
            pop r15
            pop rdi
            pop rsi
            pop rbx
            ret
        """, va)

    quest_tick = p.add_cave(build_tick)
    site = p.va(clock.start(1))
    replay = clock.group(1)
    p.check(len(replay) == 13, "quest: clock site is not movss + addss")

    def build_tick_hook(va):
        return p.asm(f"""
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
            call {quest_tick:#x}
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
            movss xmm7, dword ptr [r13 + {clock_off:#x}]
            addss xmm7, xmm6
            jmp {site + 13:#x}
        """, va)

    tick_hook = p.add_cave(build_tick_hook)
    p.write(site, p.asm(f"jmp {tick_hook:#x}", site).ljust(13, b"\x90"), expect=replay)

    # client: once register the cvar, then move a command from it into the mailbox
    def build_bridge(va):
        return p.asm(f"""
            lea rax, [rip + {block - (va + 7):#x}]
            push rbx
            push rsi
            push rdi
            sub rsp, 0x40
            mov rbx, rcx
            mov rsi, rax
            cmp dword ptr [rsi + {REG}], 0
            jne registered
            mov dword ptr [rsi + {REG}], 1
            mov qword ptr [rsp + 0x20], 1
            mov rcx, rbx
            lea rdx, [rsi + {NAME}]
            mov r8d, {len(CVAR)}
            lea r9, [rsi + {ZERO}]
            call {reg:#x}
        registered:
            cmp dword ptr [rsi + {MAIL}], 0
            jne out
            lea rcx, [rsp + 0x28]
            mov rdx, rbx
            lea r8, [rsi + {NAME}]
            mov r9d, {len(CVAR)}
            call {read:#x}
            cmp qword ptr [rsp + 0x28], -1
            je out
            mov rcx, qword ptr [rsp + 0x38]
            test rcx, rcx
            jz release
            mov rdx, qword ptr [rsp + 0x30]
            cmp rcx, 1
            jne take
            cmp byte ptr [rdx], 0x30
            je release
        take:
            cmp rcx, {BUF_MAX}
            jbe fits
            mov ecx, {BUF_MAX}
        fits:
            lea rdi, [rsi + {BUF}]
            mov byte ptr [rdi + rcx], 0
            mov r8, rsi
            mov rsi, rdx
            rep movsb
            mov rsi, r8
            mov dword ptr [rsi + {MAIL}], 1
            mov qword ptr [rsp + 0x20], 1
            mov rcx, rbx
            lea rdx, [rsi + {NAME}]
            mov r8d, {len(CVAR)}
            lea r9, [rsi + {ZERO}]
            call {put:#x}
        release:
            mov rdx, qword ptr [rsp + 0x28]
            test rdx, rdx
            jz out
            mov rcx, qword ptr [rsp + 0x30]
            mov r8d, 1
            call {dealloc:#x}
        out:
            add rsp, 0x40
            pop rdi
            pop rsi
            pop rbx
            ret
        """, va)

    bridge = p.add_cave(build_bridge)

    def build_cvar_hook(va):
        code = p.asm(f"""
            push rcx
            push rdx
            push r8
            push r9
            sub rsp, 0x28
            mov rcx, rdx
            call {bridge:#x}
            add rsp, 0x28
            pop r9
            pop r8
            pop rdx
            pop rcx
        """, va) + prologue
        return code + p.asm(f"jmp {system + len(prologue):#x}", va + len(code))

    cvar_hook = p.add_cave(build_cvar_hook)
    p.write(system, p.asm(f"jmp {cvar_hook:#x}", system).ljust(len(prologue), b"\x90"), expect=prologue)
    p.meta["quest_rva"] = block - p.ib
    p.log.append(f"quest: tick hook {site:x}, cvar system {system:x}, roller {roller:x}, block {block:x}")
