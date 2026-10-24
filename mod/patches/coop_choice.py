"""Co-op selection shield, installed transactionally by coop_ghost.patch.

AZSCHOI1 is separate from the versioned ghost block. Only a companion which has
seen the peer's capability marker enables it; old companions leave it zero.
Local deadlines use REAL_T (tick dt), never the guest's corrected run clock.
The roller counts actual new windows even when phase 2 never reaches the relay.
"""
import struct

MAGIC, SIZE = b"AZSCHOI1", 160
ENABLED, PEER, SERIAL, PHASE, REMAIN, NOW, SEQ, FLAGS, PEER_SERIAL, PEER_REMAIN = range(8, 48, 4)
WINDOW_AT, CHAIN_AT, CLOSED_AT, PREV, SEEN_SERIAL, SEEN_SEQ = range(48, 72, 4)
PEER_AT, PEER_TO, PEER_SEEN, GRACE_TO, OVERLAPPED, LAST_FLAGS, NOTE_SERIAL, EFFECTIVE_PEER = range(72, 104, 4)
K_FOUR, K_EIGHT, K_FIVE, K_ONE, K_THREE, K_TENTH, K_COVER_IN, K_COVER_OUT = range(104, 136, 4)
K_IMMUNITY = 136  # per-choice-window invulnerability cap inside the existing 160-byte block
CHOOSING, SHIELD, BOTH, COVER = 1, 2, 4, 8
LIVE = 4  # peer flag, not the local BOTH flag
OUTPUT_SEQ = 144  # exe-owned seqlock covering published serial/phase/remaining/flags
PROLOGUE = bytes.fromhex("5541574156")


def patch(p):
    """coop_ghost owns installation, layout discovery, and optional rollback."""


def prepare(p, hero_damage, enemy_damage, roller):
    for fn in (hero_damage, enemy_damage, roller):
        p.check(bytes(p.d[p.off(fn):p.off(fn) + 5]) == PROLOGUE,
                f"choice shield: prologue changed at {fn:x}")


def allocate(p, **g):
    data = bytearray(SIZE)
    data[:8] = MAGIC
    for off in (WINDOW_AT, CHAIN_AT, CLOSED_AT):
        struct.pack_into("<f", data, off, -1.0)
    struct.pack_into("<8f", data, K_FOUR, 4, 8, 5, 1, 3, .1, .8, 1.5)
    struct.pack_into("<f", data, K_IMMUNITY, 5.0)
    g["choice"] = p.add_data(SIZE, data)
    g["texts"] = []
    for pair in (("Invulnerable 5 s", "Неуязвимость: 5 с"), ("Ally is choosing - Cover", "Союзник выбирает — Прикрытие")):
        texts = []
        for text in pair:
            encoded = text.encode("utf-8")
            texts.append((p.add_data(len(encoded), encoded), len(encoded)))
        g["texts"].append(texts)
    p.meta.update(choice_shield=1, choice_invulnerable=1,
                  choice_invulnerability_seconds=5,
                  choice_rva=g["choice"] - p.ib, choice_size=SIZE)
    g["update"] = p.add_cave(lambda va: p.asm(update_code(g, va), va))
    return g


def active(g, base, no):
    """base points to AZSCHOI1. A shared defeat must bypass all reductions."""
    return f"""
        cmp dword ptr [{base} + {ENABLED}], 0
        je {no}
        test byte ptr [{base} + {g['block'] + g['B_CONTROL'] - g['choice']:#x}], {g['COOP_LIVE']}
        jz {no}
    """


def selecting(g, base, run, yes, no):
    return f"""
        cmp byte ptr [{base} + {g['state'] + g['FAKED'] - g['choice']:#x}], 2
        je {yes}
        cmp byte ptr [{run} + {g['phase']:#x}], 2
        je {yes}
        jmp {no}
    """


def within_immunity_window(g, base, run, yes, no):
    """Require the current choice phase and a REAL_T age below five seconds."""
    return f"""
        cmp byte ptr [{base} + {g['state'] + g['FAKED'] - g['choice']:#x}], 2
        je cs_immune_timer
        cmp byte ptr [{run} + {g['phase']:#x}], 2
        jne {no}
    cs_immune_timer:
        movss xmm0, dword ptr [{base} + {WINDOW_AT}]
        addss xmm0, dword ptr [{base} + {K_IMMUNITY}]
        ucomiss xmm0, dword ptr [{base} + {NOW}]
        jp {no}
        jbe {no}
        jmp {yes}
    """


def hook_damage(p, g):
    """Guard the whole own-hit path before stats, procs, logs, or enemy deaths.

    The hit-site hook still applies cover to the final xmm8 amount; this entry
    guard avoids treating a zero hit as a hit at all. Replays/sync remain live.
    Both entries start with three pushes; no RIP relocation is needed.
    """
    choice = g["choice"]
    def enemy(va):
        return p.asm(f"""
            lea r11, [rip + {choice - (va + 7):#x}]
            {active(g, 'r11', 'original')}
            cmp byte ptr [r11 + {g['state'] + g['REPLAYING'] - choice:#x}], 0
            jne original
            {selecting(g, 'r11', 'rcx', 'blocked', 'original')}
        blocked:
            ret
        original:
            push rbp
            push r15
            push r14
            jmp {g['enemy_damage'] + 5:#x}
        """, va)

    def hero(va):
        return p.asm(f"""
            lea r11, [rip + {choice - (va + 7):#x}]
            {active(g, 'r11', 'original')}
            cmp byte ptr [r11 + {g['state'] + g['DEFEAT_HIT'] - choice:#x}], 0
            jne original
            {within_immunity_window(g, 'r11', 'rcx', 'immune', 'timed')}
        immune:
            xorps xmm1, xmm1
            ret
        timed:
            test byte ptr [r11 + {FLAGS}], {SHIELD}
            jz grace
            jmp reduction
        grace:
            movss xmm0, dword ptr [r11 + {GRACE_TO}]
            ucomiss xmm0, dword ptr [r11 + {NOW}]
            ja reduction
            test byte ptr [r11 + {FLAGS}], {COVER}
            jz original
            mulss xmm1, dword ptr [r11 + {K_COVER_IN}]
            jmp original
        reduction:
            mulss xmm1, dword ptr [r11 + {K_TENTH}]
        original:
            push rbp
            push r15
            push r14
            jmp {g['hero_damage'] + 5:#x}
        """, va)
    for name, build in (("enemy_damage", enemy), ("hero_damage", hero)):
        site = g[name]
        cave = p.add_cave(build)
        p.write(site, p.asm(f"jmp {cave:#x}", site), expect=PROLOGUE)
        p.meta[f"choice_{name}_hook"] = cave - p.ib
        p.log.append(f"coop: choice {name} {site:x} -> {cave:x}")


def outgoing(g, state):
    """At hit capture: r11 is the ghost state, xmm8 is final damage."""
    return f"""
        cmp dword ptr [r11 + {g['choice'] + ENABLED - state:#x}], 0
        je cs_hit_done
        test byte ptr [r11 + {g['choice'] + FLAGS - state:#x}], {COVER}
        jz cs_hit_done
        mulss xmm8, dword ptr [r11 + {g['choice'] + K_COVER_OUT - state:#x}]
    cs_hit_done:
    """


def roller_code(g, trampoline, va):
    """Called after the existing FAKED gate, with the stock roller's arguments."""
    return f"""
        lea r11, [rip + {g['choice'] - (va + 7):#x}]
        push rbx
        push rsi
        sub rsp, 0x38
        mov rbx, rcx
        mov rsi, r11
        mov byte ptr [rsp + 0x20], r8b
        call {trampoline:#x}
        mov qword ptr [rsp + 0x28], rax
        cmp byte ptr [rsp + 0x20], 0
        je done
        cmp byte ptr [rbx + {g['phase']:#x}], 2
        jne done
        inc dword ptr [rsi + {OUTPUT_SEQ}]
        inc dword ptr [rsi + {SERIAL}]
        mov rcx, rbx
        call {g['update']:#x}
    done:
        mov rax, qword ptr [rsp + 0x28]
        add rsp, 0x38
        pop rsi
        pop rbx
        ret
    """


def update_code(g, va):
    """rcx run state; preserve every nonvolatile and xmm0..2 (wrapper's dt).

    Two independently bounded windows; overlapping choice latches until *my*
    window closes. Peer duplicates can shorten a deadline, never extend it.
    """
    return _update_body(g, va)


def _update_body(g, va):
    # movabs is intentional: Patcher relocates absolute image pointers via its
    # normal relocation table only when registered; use RIP lea as a prefix.
    prefix = f"lea r11, [rip + {g['choice'] - (va + 7):#x}]\n"
    return prefix + f"""
        push rbx
        push rsi
        push rdi
        push r12
        push rdx
        push r8
        push r9
        push r10
        push rax
        push r11
        sub rsp, 0x98
        movups xmmword ptr [rsp + 0x60], xmm0
        movups xmmword ptr [rsp + 0x70], xmm1
        movups xmmword ptr [rsp + 0x80], xmm2
        mov rsi, rcx
        mov rbx, r11
        test byte ptr [rbx + {OUTPUT_SEQ}], 1
        jnz cs_writing
        inc dword ptr [rbx + {OUTPUT_SEQ}]
    cs_writing:
        lea rdi, [rbx + {g['state'] - g['choice']:#x}]
        lea r12, [rbx + {g['block'] - g['choice']:#x}]
        movss xmm0, dword ptr [rdi + {g['REAL_T']}]
        movss dword ptr [rbx + {NOW}], xmm0
        movzx eax, byte ptr [rsi + {g['phase']:#x}]
        cmp byte ptr [rdi + {g['FAKED']}], 0
        je cs_phase
        movzx eax, byte ptr [rdi + {g['FAKED']}]
    cs_phase:
        mov dword ptr [rbx + {PHASE}], eax
        mov edx, dword ptr [rbx + {FLAGS}]
        mov dword ptr [rbx + {LAST_FLAGS}], edx
        mov dword ptr [rbx + {FLAGS}], 0
        mov dword ptr [rbx + {REMAIN}], 0
        {active(g, 'rbx', 'cs_reset')}
        cmp eax, 1
        jb cs_reset
        cmp eax, 3
        ja cs_reset
        mov eax, dword ptr [rbx + {SEQ}]
        cmp eax, dword ptr [rbx + {SEEN_SEQ}]
        je cs_peer
        mov dword ptr [rbx + {SEEN_SEQ}], eax
        movss dword ptr [rbx + {PEER_AT}], xmm0
        mov eax, dword ptr [rbx + {PEER_SERIAL}]
        cmp eax, dword ptr [rbx + {PEER_SEEN}]
        je cs_peer_same
        mov dword ptr [rbx + {PEER_SEEN}], eax
        movaps xmm2, xmm0
        addss xmm2, dword ptr [rbx + {PEER_REMAIN}]
        movss dword ptr [rbx + {PEER_TO}], xmm2
        jmp cs_peer
    cs_peer_same:
        movaps xmm2, xmm0
        addss xmm2, dword ptr [rbx + {PEER_REMAIN}]
        minss xmm2, dword ptr [rbx + {PEER_TO}]
        movss dword ptr [rbx + {PEER_TO}], xmm2
    cs_peer:
        mov edx, dword ptr [rbx + {PEER}]
        movaps xmm2, xmm0
        subss xmm2, dword ptr [rbx + {PEER_AT}]
        ucomiss xmm2, dword ptr [rbx + {K_THREE}]
        jb cs_peer_fresh
        xor edx, edx
    cs_peer_fresh:
        test edx, {LIVE}
        jnz cs_peer_live
        xor edx, edx
    cs_peer_live:
        movss xmm2, dword ptr [rbx + {PEER_TO}]
        ucomiss xmm2, xmm0
        ja cs_peer_save
        and edx, {~SHIELD & 0xffffffff}
    cs_peer_save:
        mov dword ptr [rbx + {EFFECTIVE_PEER}], edx
        cmp dword ptr [rbx + {PHASE}], 2
        jne cs_closed
        or byte ptr [rbx + {FLAGS}], {CHOOSING}
        cmp dword ptr [rbx + {PREV}], 0
        je cs_open
        mov eax, dword ptr [rbx + {SERIAL}]
        cmp eax, dword ptr [rbx + {SEEN_SERIAL}]
        je cs_window
    cs_open:
        mov dword ptr [rbx + {GRACE_TO}], 0
        mov dword ptr [rbx + {OVERLAPPED}], 0
        xorps xmm2, xmm2
        ucomiss xmm2, dword ptr [rbx + {CHAIN_AT}]
        ja cs_chain_new
        ucomiss xmm2, dword ptr [rbx + {CLOSED_AT}]
        ja cs_chain_keep
        movaps xmm2, xmm0
        subss xmm2, dword ptr [rbx + {CLOSED_AT}]
        ucomiss xmm2, dword ptr [rbx + {K_FIVE}]
        jb cs_chain_keep
    cs_chain_new:
        movss dword ptr [rbx + {CHAIN_AT}], xmm0
    cs_chain_keep:
        movss dword ptr [rbx + {WINDOW_AT}], xmm0
        mov dword ptr [rbx + {CLOSED_AT}], 0xbf800000
        mov eax, dword ptr [rbx + {SERIAL}]
        mov dword ptr [rbx + {SEEN_SERIAL}], eax
    cs_window:
        mov dword ptr [rbx + {PREV}], 1
        mov eax, edx
        and eax, {LIVE | CHOOSING}
        cmp eax, {LIVE | CHOOSING}
        jne cs_deadline
        or byte ptr [rbx + {FLAGS}], {BOTH}
        mov dword ptr [rbx + {OVERLAPPED}], 1
    cs_deadline:
        movss xmm1, dword ptr [rbx + {WINDOW_AT}]
        addss xmm1, dword ptr [rbx + {K_FOUR}]
        movss xmm2, dword ptr [rbx + {CHAIN_AT}]
        addss xmm2, dword ptr [rbx + {K_EIGHT}]
        minss xmm1, xmm2
        subss xmm1, xmm0
        xorps xmm2, xmm2
        ucomiss xmm1, xmm2
        jbe cs_notes
        movss dword ptr [rbx + {REMAIN}], xmm1
        or byte ptr [rbx + {FLAGS}], {SHIELD}
        jmp cs_notes
    cs_closed:
        cmp dword ptr [rbx + {PREV}], 0
        je cs_cover
        movss dword ptr [rbx + {CLOSED_AT}], xmm0
        cmp dword ptr [rbx + {OVERLAPPED}], 0
        je cs_end_window
        movaps xmm2, xmm0
        addss xmm2, dword ptr [rbx + {K_ONE}]
        movss dword ptr [rbx + {GRACE_TO}], xmm2
    cs_end_window:
        mov dword ptr [rbx + {PREV}], 0
        mov dword ptr [rbx + {OVERLAPPED}], 0
    cs_cover:
        mov eax, edx
        and eax, {LIVE | CHOOSING | SHIELD}
        cmp eax, {LIVE | CHOOSING | SHIELD}
        jne cs_notes
        or byte ptr [rbx + {FLAGS}], {COVER}
    cs_notes:
        {notes_code(g)}
        jmp cs_return
    cs_reset:
        mov dword ptr [rbx + {PREV}], 0
        mov dword ptr [rbx + {CHAIN_AT}], 0xbf800000
        mov dword ptr [rbx + {CLOSED_AT}], 0xbf800000
        mov dword ptr [rbx + {GRACE_TO}], 0
        mov dword ptr [rbx + {OVERLAPPED}], 0
    cs_return:
        inc dword ptr [rbx + {OUTPUT_SEQ}]
        movups xmm0, xmmword ptr [rsp + 0x60]
        movups xmm1, xmmword ptr [rsp + 0x70]
        movups xmm2, xmmword ptr [rsp + 0x80]
        add rsp, 0x98
        pop r11
        pop rax
        pop r10
        pop r9
        pop r8
        pop rdx
        pop r12
        pop rdi
        pop rsi
        pop rbx
        ret
    """


def notes_code(g):
    if not g.get("announce"):
        return ""
    def send(n, tag):
        en, ru = g["texts"][n]
        return f"""
            lea r9, [rbx + {en[0] - g['choice']:#x}]
            mov eax, {en[1]}
            cmp byte ptr [r12 + {g['B_LANG']}], 1
            jne {tag}_text
            lea r9, [rbx + {ru[0] - g['choice']:#x}]
            mov eax, {ru[1]}
        {tag}_text:
            lea rcx, [rsp + 0x40]
            mov rdx, r9
            xor r8d, r8d
            mov qword ptr [rsp + 0x20], rax
            call {g['announce']:#x}
            lea rcx, [rsi + {g['queue']:#x}]
            mov rax, qword ptr [rcx + 0x10]
            cmp rax, qword ptr [rcx]
            jne {tag}_room
            call {g['grow']:#x}
            lea rcx, [rsi + {g['queue']:#x}]
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
    return f"""
        test byte ptr [rbx + {FLAGS}], {SHIELD}
        jz cs_cover_note
        mov eax, dword ptr [rbx + {SERIAL}]
        cmp eax, dword ptr [rbx + {NOTE_SERIAL}]
        jne cs_own_note
        test byte ptr [rbx + {LAST_FLAGS}], {SHIELD}
        jnz cs_cover_note
    cs_own_note:
        mov dword ptr [rbx + {NOTE_SERIAL}], eax
        {send(0, 'cs_note_own')}
    cs_cover_note:
        test byte ptr [rbx + {FLAGS}], {COVER}
        jz cs_note_done
        test byte ptr [rbx + {LAST_FLAGS}], {COVER}
        jnz cs_note_done
        {send(1, 'cs_note_cover')}
    cs_note_done:
    """


def install_hold(p, g, sh):
    """Stock MONSTER_MOVE(from=current,to=current), then skip the AI turn.

    rcx run state, rdx record. Preserve all volatile registers and xmm0..5 so
    the loop's register allocation is untouched. Do not alter boss records.
    """
    f = g['full']
    def build(va):
        return p.asm(f"""
            push rax
            push rcx
            push rdx
            push r8
            push r9
            push r10
            push r11
            push rbx
            push rsi
            sub rsp, 0xe0
            movups xmmword ptr [rsp + 0x80], xmm0
            movups xmmword ptr [rsp + 0x90], xmm1
            movups xmmword ptr [rsp + 0xa0], xmm2
            movups xmmword ptr [rsp + 0xb0], xmm3
            movups xmmword ptr [rsp + 0xc0], xmm4
            movups xmmword ptr [rsp + 0xd0], xmm5
            mov rbx, rcx
            mov rsi, rdx
            movss xmm0, dword ptr [rbx + {g['clock']:#x}]
            subss xmm0, dword ptr [rsi + {f['start']:#x}]
            movss xmm1, dword ptr [rsi + {f['dur']:#x}]
            xorps xmm2, xmm2
            ucomiss xmm1, xmm2
            jbe hold_end
            divss xmm0, xmm1
            maxss xmm0, xmm2
            mov eax, 0x3f800000
            movd xmm1, eax
            minss xmm0, xmm1
            jmp hold_lerp
        hold_end:
            mov eax, 0x3f800000
            movd xmm0, eax
        hold_lerp:
            movss xmm3, xmm0
            shufps xmm0, xmm0, 0
            movsd xmm1, qword ptr [rsi + {f['from']:#x}]
            movsd xmm2, qword ptr [rsi + {f['to']:#x}]
            subps xmm2, xmm1
            mulps xmm2, xmm0
            addps xmm1, xmm2
            movsd qword ptr [rsp + 0x60], xmm1
            movss xmm2, dword ptr [rsi + {f['to'] + 8:#x}]
            subss xmm2, dword ptr [rsi + {f['from'] + 8:#x}]
            mulss xmm2, xmm3
            addss xmm2, dword ptr [rsi + {f['from'] + 8:#x}]
            movss dword ptr [rsp + 0x68], xmm2
            movsd qword ptr [rsi + {f['from']:#x}], xmm1
            movsd qword ptr [rsi + {f['to']:#x}], xmm1
            movss dword ptr [rsi + {f['from'] + 8:#x}], xmm2
            movss dword ptr [rsi + {f['to'] + 8:#x}], xmm2
            mov eax, dword ptr [rbx + {g['clock']:#x}]
            mov dword ptr [rsi + {f['start']:#x}], eax
            mov dword ptr [rsi + {f['dur']:#x}], 0x3d4ccccd
            inc dword ptr [rbx + {sh['spline']:#x}]
            mov eax, dword ptr [rbx + {sh['spline']:#x}]
            mov dword ptr [rsp + 0x20], eax
            mov dword ptr [rsp + 0x28], 50
            lea rcx, [rsp + 0x40]
            mov rdx, qword ptr [rsi + {f['guid']:#x}]
            lea r8, [rsp + 0x60]
            mov r9, r8
            call {sh['builder']:#x}
            lea rcx, [rbx + {g['queue']:#x}]
            mov rax, qword ptr [rcx + 0x10]
            cmp rax, qword ptr [rcx]
            jne hold_room
            call {g['grow']:#x}
            lea rcx, [rbx + {g['queue']:#x}]
            mov rax, qword ptr [rcx + 0x10]
        hold_room:
            shl rax, 5
            add rax, qword ptr [rcx + 8]
            movups xmm0, xmmword ptr [rsp + 0x40]
            movups xmmword ptr [rax], xmm0
            movups xmm0, xmmword ptr [rsp + 0x50]
            movups xmmword ptr [rax + 0x10], xmm0
            inc qword ptr [rcx + 0x10]
            movups xmm0, xmmword ptr [rsp + 0x80]
            movups xmm1, xmmword ptr [rsp + 0x90]
            movups xmm2, xmmword ptr [rsp + 0xa0]
            movups xmm3, xmmword ptr [rsp + 0xb0]
            movups xmm4, xmmword ptr [rsp + 0xc0]
            movups xmm5, xmmword ptr [rsp + 0xd0]
            add rsp, 0xe0
            pop rsi
            pop rbx
            pop r11
            pop r10
            pop r9
            pop r8
            pop rdx
            pop rcx
            pop rax
            ret
        """, va)
    cave = p.add_cave(build)
    p.meta['choice_hold_hook'] = cave - p.ib
    p.log.append(f"coop: choice hold {cave:x}, stock movement builder {sh['builder']:x}")
    return cave
