"""Filter local hero cast visuals during the native phase-2 selection window.

Install the returned cave immediately after the stock run-tick call in the
coop run-sync wrapper, before that wrapper restores FAKED. The cave accepts the
run-state pointer in RCX. It removes only opcode 0x132 whose payload begins with
the caster packed GUID twice, followed by a u32 spell id; the first caster must
match hero_guid.

This deliberately does not filter summon GUIDs or any unverified opcode.
"""

import struct

from patches import coop_choice as choice


SPELL_GO = 0x132


def patch(p):
    """Installed by coop_ghost after its layout and choice checks succeed."""


def decode_spell_go_caster(payload):
    """Decode the first packed caster GUID from the native SPELL_GO layout.

    The native builder writes packed caster, packed caster again, then a u32
    spell id. Return None unless both packed GUIDs and that spell id fit.
    """
    view = memoryview(payload)

    def read_guid(cursor):
        if cursor >= len(view):
            return None
        mask = view[cursor]
        cursor += 1
        needed = mask.bit_count()
        if cursor + needed > len(view):
            return None
        guid = 0
        for byte_index in range(8):
            if mask & (1 << byte_index):
                guid |= view[cursor] << (8 * byte_index)
                cursor += 1
        return guid, cursor

    first = read_guid(0)
    if first is None:
        return None
    second = read_guid(first[1])
    if second is None or second[1] + 4 > len(view):
        return None
    return first[0]


def is_own_spell_go(opcode, payload, hero_guid):
    """Whether a complete outgoing payload is this hero's SPELL_GO."""
    return opcode == SPELL_GO and decode_spell_go_caster(payload) == hero_guid


def install(p, g, slots, dealloc):
    """Create the post-tick queue filter cave.

    `g` is the co-op layout dictionary from coop_ghost.patch; `slots` is the
    result of find_damage_slots; `dealloc` is find_dealloc's validated target.
    The caller must call this cave only after the native run tick returns and
    before FAKED is restored.
    """
    required = ("choice", "block", "state", "queue", "phase", "B_CONTROL", "COOP_LIVE", "FAKED", "REPLAYING")
    p.check(all(k in g for k in required) and "hero_guid" in slots,
            "choice cast filter: missing validated layout")
    queue, state, block, choice_base = g["queue"], g["state"], g["block"], g["choice"]
    hero_guid = slots["hero_guid"]

    def build(va):
        return p.asm(f"""
            push r11
            lea r11, [rip + {state - (va + 9):#x}]
            push rax
            push rcx
            push rdx
            push r8
            push r9
            push r10
            push rbx
            push rsi
            push rdi
            push r12
            push r13
            push r14
            push r15
            mov rbx, rcx
            sub rsp, 0x88
            movdqu xmmword ptr [rsp + 0x20], xmm0
            movdqu xmmword ptr [rsp + 0x30], xmm1
            movdqu xmmword ptr [rsp + 0x40], xmm2
            movdqu xmmword ptr [rsp + 0x50], xmm3
            movdqu xmmword ptr [rsp + 0x60], xmm4
            movdqu xmmword ptr [rsp + 0x70], xmm5
            cmp byte ptr [r11 + {g['FAKED']}], 2
            je qf_active
            cmp byte ptr [rbx + {g['phase']}], 2
            jne qf_done
        qf_active:
            cmp byte ptr [r11 + {g['REPLAYING']}], 0
            jne qf_done
            lea r10, [r11 + {choice_base - state:#x}]
            cmp dword ptr [r10 + {choice.ENABLED}], 0
            je qf_done
            lea rax, [r11 + {block - state:#x}]
            test byte ptr [rax + {g['B_CONTROL']}], {g['COOP_LIVE']}
            jz qf_done

            mov r13, qword ptr [rbx + {queue + 8:#x}]
            mov r14, qword ptr [rbx + {queue + 0x10:#x}]
            test r14, r14
            jz qf_done
            test r13, r13
            jz qf_done
            xor r12d, r12d
        qf_loop:
            cmp r12, r14
            jae qf_done
            mov r15, r12
            shl r15, 5
            add r15, r13
            cmp word ptr [r15 + 0x18], {SPELL_GO:#x}
            jne qf_keep
            mov rdi, qword ptr [r15 + 8]
            test rdi, rdi
            jz qf_keep
            mov r11, qword ptr [r15 + 0x10]
            cmp r11, 1
            jb qf_keep
            cmp qword ptr [r15], r11
            jb qf_keep

            movzx esi, byte ptr [rdi]
            lea r10, [rdi + 1]
            xor eax, eax
            xor ecx, ecx
            xor edx, edx
            mov r8d, 8
        qf_guid:
            test sil, 1
            jz qf_guid_next
            lea r9, [rdx + 1]
            cmp r9, r11
            jae qf_keep
            movzx r9d, byte ptr [r10 + rdx]
            shl r9, cl
            or rax, r9
            inc edx
        qf_guid_next:
            shr sil, 1
            add ecx, 8
            dec r8d
            jnz qf_guid
            mov r8, rax
            lea r9, [rdx + 1]
            cmp r9, r11
            jae qf_keep
            movzx esi, byte ptr [rdi + r9]
            lea ecx, [r9 + 1]
            mov r9d, 8
        qf_guid2:
            test sil, 1
            jz qf_guid2_next
            inc ecx
        qf_guid2_next:
            shr sil, 1
            dec r9d
            jnz qf_guid2
            lea rdx, [rcx + 4]
            cmp rdx, r11
            ja qf_keep
            cmp r8, qword ptr [rbx + {hero_guid:#x}]
            jne qf_keep

            mov rcx, rdi
            mov rdx, qword ptr [r15]
            test rdx, rdx
            jz qf_keep
            test rcx, rcx
            jz qf_keep
            mov r8d, 1
            call {dealloc:#x}
        qf_compact:
            lea rsi, [r15 + 0x20]
            mov rdi, r15
            mov rax, r14
            shl rax, 5
            lea rdx, [r13 + rax]
        qf_copy:
            cmp rsi, rdx
            jae qf_removed
            mov rax, qword ptr [rsi]
            mov qword ptr [rdi], rax
            add rsi, 8
            add rdi, 8
            jmp qf_copy
        qf_removed:
            dec r14
            mov qword ptr [rbx + {queue + 0x10:#x}], r14
            jmp qf_loop
        qf_keep:
            inc r12
            jmp qf_loop
        qf_done:
            movdqu xmm0, xmmword ptr [rsp + 0x20]
            movdqu xmm1, xmmword ptr [rsp + 0x30]
            movdqu xmm2, xmmword ptr [rsp + 0x40]
            movdqu xmm3, xmmword ptr [rsp + 0x50]
            movdqu xmm4, xmmword ptr [rsp + 0x60]
            movdqu xmm5, xmmword ptr [rsp + 0x70]
            add rsp, 0x88
            pop r15
            pop r14
            pop r13
            pop r12
            pop rdi
            pop rsi
            pop rbx
            pop r10
            pop r9
            pop r8
            pop rdx
            pop rcx
            pop rax
            pop r11
            ret
        """, va)

    cave = p.add_cave(build)
    p.meta["choice_cast_filter_rva"] = cave - p.ib
    return cave
