"""Move nearby ordinary enemies out of the hero's space when a choice ends.

Installed by coop_ghost after the choice state update, with rcx = RunState.
All address inputs are resolved by the existing game-layout finders.
"""
from patches import coop_choice as choice_api
from patches import coop_shared as shared_api


def patch(p):
    """coop_ghost installs this helper after discovering the full run layout."""


def install(p, g, sh):
    """Install the post-choice tick cave and return its entry address.

    `g` is the choice/ghost layout plus `hero` (RunState XYZ offset) and
    `peer_position` (absolute address of the peer's published XYZ). `sh` is
    the validated shared-enemy record and stock movement-builder layout.
    """
    f = g['full']
    # The cave only edits ordinary live records. These offsets all come from
    # the validated enemy initialiser / boss layout passed by coop_ghost.
    required = ('hero', 'peer_position', 'choice', 'block', 'B_CONTROL',
                'COOP_LIVE', 'phase', 'clock', 'queue', 'grow', 'full')
    p.check(all(k in g for k in required), 'choice push: incomplete run layout')
    p.check(all(k in sh for k in ('boss', 'guard', 'add', 'dead', 'owner', 'spline', 'builder')),
            'choice push: incomplete enemy layout')

    def build(va):
        choice_base = g['choice']
        def process(label, center_load):
            return f"""
        {label}_center:
            {center_load}
            mov rsi, qword ptr [rbx + {f['enemies']:#x}]
            mov r12, qword ptr [rbx + {f['enemies'] + 8:#x}]
            test rsi, rsi
            jz {label}_done
            cmp r12, 4096
            ja {label}_done
        {label}_loop:
            test r12, r12
            jz {label}_done
            cmp dword ptr [rsi + {sh['dead']:#x}], 0
            jne {label}_next
            cmp byte ptr [rsp + 0xe2], 0
            je {label}_owner_ok
            test byte ptr [rsi + {sh['owner']:#x}], 1
            jnz {label}_next
        {label}_owner_ok:
            cmp qword ptr [rsi + {sh['boss']:#x}], -1
            jne {label}_next
            cmp qword ptr [rsi + {sh['guard']:#x}], -1
            jne {label}_next
            cmp dword ptr [rsi + {sh['add']:#x}], -1
            jne {label}_next

            # Clamp the current movement interpolation to [0, 1].
            movss xmm0, dword ptr [rbx + {g['clock']:#x}]
            subss xmm0, dword ptr [rsi + {f['start']:#x}]
            movss xmm1, dword ptr [rsi + {f['dur']:#x}]
            xorps xmm2, xmm2
            ucomiss xmm1, xmm2
            jbe {label}_at_end
            divss xmm0, xmm1
            maxss xmm0, xmm2
            mov eax, 0x3f800000
            movd xmm1, eax
            minss xmm0, xmm1
            jmp {label}_lerp
        {label}_at_end:
            mov eax, 0x3f800000
            movd xmm0, eax
        {label}_lerp:
            movss xmm1, dword ptr [rsi + {f['to']:#x}]
            subss xmm1, dword ptr [rsi + {f['from']:#x}]
            mulss xmm1, xmm0
            addss xmm1, dword ptr [rsi + {f['from']:#x}]
            movss xmm2, dword ptr [rsi + {f['to'] + 4:#x}]
            subss xmm2, dword ptr [rsi + {f['from'] + 4:#x}]
            mulss xmm2, xmm0
            addss xmm2, dword ptr [rsi + {f['from'] + 4:#x}]
            movss xmm3, dword ptr [rsi + {f['to'] + 8:#x}]
            subss xmm3, dword ptr [rsi + {f['from'] + 8:#x}]
            mulss xmm3, xmm0
            addss xmm3, dword ptr [rsi + {f['from'] + 8:#x}]
            movss dword ptr [rsp + 0x5c], xmm3
            movss dword ptr [rsp + 0xd0], xmm1
            movss dword ptr [rsp + 0xd4], xmm2
            movss dword ptr [rsp + 0xd8], xmm3

            # Squared XY distance must be strictly below 12 yards.
            movss xmm4, xmm1
            subss xmm4, dword ptr [rsp + 0x60]
            movss xmm5, xmm2
            subss xmm5, dword ptr [rsp + 0x64]
            movaps xmm0, xmm4
            mulss xmm0, xmm4
            mulss xmm5, xmm5
            addss xmm0, xmm5
            mov eax, 0x43100000
            movd xmm5, eax
            ucomiss xmm0, xmm5
            jp {label}_next
            jae {label}_next

            # Move exactly five yards outward from the current interpolated
            # point; exact center deterministically chooses +X. Keep Z.
            sqrtss xmm0, xmm0
            xorps xmm5, xmm5
            ucomiss xmm0, xmm5
            je {label}_zero
            mov eax, 0x40a00000
            movd xmm5, eax
            divss xmm5, xmm0
            subss xmm1, dword ptr [rsp + 0x60]
            mulss xmm1, xmm5
            addss xmm1, dword ptr [rsp + 0xd0]
            subss xmm2, dword ptr [rsp + 0x64]
            mulss xmm2, xmm5
            addss xmm2, dword ptr [rsp + 0xd4]
            jmp {label}_target
        {label}_zero:
            movss xmm1, dword ptr [rsp + 0xd0]
            mov eax, 0x40a00000
            movd xmm5, eax
            addss xmm1, xmm5
            movss xmm2, dword ptr [rsp + 0xd4]
        {label}_target:
            movss xmm3, dword ptr [rsp + 0x5c]
            movss dword ptr [rsp + 0x50], xmm1
            movss dword ptr [rsp + 0x54], xmm2
            movss dword ptr [rsp + 0x58], xmm3
            movss dword ptr [rsi + {f['from']:#x}], xmm1
            movss dword ptr [rsi + {f['from'] + 4:#x}], xmm2
            movss dword ptr [rsi + {f['from'] + 8:#x}], xmm3
            movss dword ptr [rsi + {f['to']:#x}], xmm1
            movss dword ptr [rsi + {f['to'] + 4:#x}], xmm2
            movss dword ptr [rsi + {f['to'] + 8:#x}], xmm3
            mov eax, dword ptr [rbx + {g['clock']:#x}]
            mov dword ptr [rsi + {f['start']:#x}], eax
            mov dword ptr [rsi + {f['dur']:#x}], 0x3d4ccccd

            # Build and enqueue the game's ordinary MONSTER_MOVE packet.
            mov qword ptr [rsp + 0x30], 0
            mov qword ptr [rsp + 0x38], 0
            mov eax, dword ptr [rbx + {sh['spline']:#x}]
            inc eax
            mov dword ptr [rbx + {sh['spline']:#x}], eax
            mov dword ptr [rsp + 0x20], eax
            mov dword ptr [rsp + 0x28], 50
            lea rcx, [rsp + 0x30]
            mov rdx, qword ptr [rsi + {f['guid']:#x}]
            lea r8, [rsp + 0xd0]
            lea r9, [rsp + 0x50]
            call {sh['builder']:#x}
            lea r14, [rbx + {g['queue']:#x}]
            mov rax, qword ptr [r14 + 0x10]
            cmp rax, qword ptr [r14]
            jne {label}_room
            mov rcx, r14
            call {g['grow']:#x}
            lea r14, [rbx + {g['queue']:#x}]
            mov rax, qword ptr [r14 + 0x10]
        {label}_room:
            shl rax, 5
            add rax, qword ptr [r14 + 8]
            movups xmm0, xmmword ptr [rsp + 0x30]
            movups xmmword ptr [rax], xmm0
            movups xmm0, xmmword ptr [rsp + 0x40]
            movups xmmword ptr [rax + 0x10], xmm0
            inc qword ptr [r14 + 0x10]
        {label}_next:
            add rsi, {f['size']:#x}
            dec r12
            jmp {label}_loop
        {label}_done:
            jmp cs_push_after_{label}
        """

        local_load = f"""
            mov rax, qword ptr [rbx + {g['hero']:#x}]
            mov qword ptr [rsp + 0x60], rax
            mov eax, dword ptr [rbx + {g['hero'] + 8:#x}]
            mov dword ptr [rsp + 0x68], eax
        """
        peer_load = f"""
            lea rax, [r15 + {g['peer_position'] - choice_base:#x}]
            mov rdx, qword ptr [rax]
            mov qword ptr [rsp + 0x60], rdx
            mov edx, dword ptr [rax + 8]
            mov dword ptr [rsp + 0x68], edx
        """
        # Emit distinct passes: a host may process both the local and peer
        # edge in the same tick, each against that hero's own center.
        body = f"""
            push rbx
            push rsi
            push rdi
            push r12
            push r13
            push r14
            push r15
            lea r15, [rip + {choice_base - (va + 18):#x}]
            lea r14, [r15 + {g['block'] - choice_base:#x}]
            sub rsp, 0x110
            movups xmmword ptr [rsp + 0x70], xmm0
            movups xmmword ptr [rsp + 0x80], xmm1
            movups xmmword ptr [rsp + 0x90], xmm2
            movups xmmword ptr [rsp + 0xa0], xmm3
            movups xmmword ptr [rsp + 0xb0], xmm4
            movups xmmword ptr [rsp + 0xc0], xmm5
            mov rbx, rcx
            mov byte ptr [rsp + 0xe2], 0

            # Detect and store the local canonical 2 -> 1 phase edge.
            movzx eax, byte ptr [rbx + {g['phase']:#x}]
            mov edx, dword ptr [r15 + 148]
            mov dword ptr [r15 + 148], eax
            cmp edx, 2
            jne cs_local_no
            cmp eax, 1
            jne cs_local_no
            mov byte ptr [rsp + 0xe0], 1
            jmp cs_peer_edge
        cs_local_no:
            mov byte ptr [rsp + 0xe0], 0
        cs_peer_edge:
            # EFFECTIVE_PEER is already freshness/live filtered by choice.update.
            mov eax, dword ptr [r15 + {choice_api.EFFECTIVE_PEER}]
            xor edx, edx
            test eax, {choice_api.LIVE}
            jz cs_peer_not_choosing
            test eax, {choice_api.CHOOSING}
            jz cs_peer_not_choosing
            mov edx, 1
        cs_peer_not_choosing:
            mov ecx, dword ptr [r15 + 152]
            mov dword ptr [r15 + 152], edx
            test ecx, ecx
            jz cs_peer_no_edge
            test edx, edx
            jnz cs_peer_no_edge
            test eax, {choice_api.LIVE}
            jz cs_peer_no_edge
            mov byte ptr [rsp + 0xe1], 1
            jmp cs_push_gates
        cs_peer_no_edge:
            mov byte ptr [rsp + 0xe1], 0
        cs_push_gates:
            cmp dword ptr [r15 + {choice_api.ENABLED}], 0
            je cs_push_restore
            test byte ptr [r14 + {g['B_CONTROL']}], {g['COOP_LIVE']}
            jz cs_push_restore
            movzx eax, byte ptr [r14 + {g['B_CONTROL']}]
            mov edx, {shared_api.LIVE | shared_api.GUEST}
            and eax, edx
            cmp eax, edx
            jne cs_push_local_owner_ready
            mov byte ptr [rsp + 0xe2], 1
        cs_push_local_owner_ready:
            movzx eax, byte ptr [rsp + 0xe0]
            test eax, eax
            jz cs_push_peer_check
            {process('local', local_load)}
        cs_push_after_local:
        cs_push_peer_check:
            # Shared guests apply pushes only to records they own. Host-owned
            # records follow the host's stock movement segment over the relay.
            lea r14, [r15 + {g['block'] - choice_base:#x}]
            movzx eax, byte ptr [r14 + {g['B_CONTROL']}]
            test eax, {shared_api.LIVE}
            jz cs_push_restore
            cmp byte ptr [rsp + 0xe1], 0
            je cs_push_restore
            {process('peer', peer_load)}
        cs_push_after_peer:
        cs_push_restore:
            movups xmm0, xmmword ptr [rsp + 0x70]
            movups xmm1, xmmword ptr [rsp + 0x80]
            movups xmm2, xmmword ptr [rsp + 0x90]
            movups xmm3, xmmword ptr [rsp + 0xa0]
            movups xmm4, xmmword ptr [rsp + 0xb0]
            movups xmm5, xmmword ptr [rsp + 0xc0]
            add rsp, 0x110
            pop r15
            pop r14
            pop r13
            pop r12
            pop rdi
            pop rsi
            pop rbx
            ret
        """
        return p.asm(body, va)

    # Distances are materialized in registers, without absolute data pointers.
    cave = p.add_cave(build)
    p.meta.update(choice_push_hook=cave - p.ib,
                  choice_push_radius=12, choice_push_distance=5)
    p.log.append(f"coop: choice pushback tick {cave:x}")
    return cave
