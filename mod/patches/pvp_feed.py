"""Live run feed for the PvP companion.

Every AZS addon line the game's local server sends to the UI goes through one
builder (prefix, body). Its entry is hooked: for AZSRUN, AZSEND and AZSZONE the
body is copied into a fixed block in .moddata that the companion reads with
ReadProcessMemory (module base + rva from AzerothSurvivors-52.feed.json).

Layout: "AZSFEED1", then one 1024-byte slot per prefix in this order:
u32 seq, u32 len, 1016 bytes of body (truncated). Seqlock: seq is odd while a
write is in progress; a reader retries on an odd or changed seq.
"""
import re

SLOTS = ["AZSRUN", "AZSEND", "AZSZONE"]
SLOT = 1024
BODY = SLOT - 8
MAGIC = b"AZSFEED1"

# builder prologue: push rbp/r15/r14/rsi/rdi/rbx; sub rsp, N; lea rbp, [rsp+L]; mov [rbp+x], -2;
# mov rdi, rdx; mov rsi, rcx; movaps xmm0, [rbp+D] (body ptr/len); mov [rbp+P], r8; mov [rbp+P+8], r9
# (frame sizes and slots differ between builds: N = 0xc8 in v105, 0xa8 in v107)
BUILDER = re.compile(rb"\x55\x41\x57\x41\x56\x56\x57\x53\x48\x81\xec(.{4})\x48\x8d\xac\x24(.{4})"
                     rb"\x48\xc7\x45.\xfe\xff\xff\xff\x48\x89\xd7\x48\x89\xce\x0f\x28\x85(.{4})"
                     rb"\x4c\x89\x45(.)\x4c\x89\x4d(.)", re.S)
PROLOGUE = bytes.fromhex("554157415656" "5753")  # the 8 bytes the jmp replaces
# mov byte [rax], 6; mov dword [rax+1], -1 (chat type 6 = addon); mov word [rbp+x], 0x96 (SMSG_MESSAGECHAT)
PACKET = re.compile(rb"\xc6\x00\x06\xc7\x40\x01\xff\xff\xff\xff.{0,32}?\x66\xc7\x45.\x96\x00", re.S)


def patch(p):
    entry = p.offsets.find_unique('feed.builder', [BUILDER])
    m = BUILDER.match(p.d, p.off(entry))
    frame, rbp_at, body_disp = (int.from_bytes(m.group(i), "little", signed=True) for i in (1, 2, 3))
    prefix_lo, prefix_hi = (int.from_bytes(m.group(i), "little", signed=True) for i in (4, 5))
    # [rbp + D] = entry rsp - 0x30 (pushes) - N + L + D: the cave reads the body at entry rsp + 0x28
    p.check(body_disp - 0x30 - frame + rbp_at == 0x28, "AZS line builder: body is no longer the 5th stack argument")
    p.check(prefix_hi == prefix_lo + 8, "AZS line builder: r8/r9 are no longer the prefix ptr/len")
    p.check(PACKET.search(p.d, m.end(), m.start() + 0x100) is not None,
            "AZS line builder: not the addon message packet")

    feed = p.add_data(len(MAGIC) + SLOT * len(SLOTS), MAGIC)
    slot = [feed + len(MAGIC) + SLOT * i for i in range(len(SLOTS))]
    # at entry: r8/r9 = prefix ptr/len, [rsp+0x28]/[rsp+0x30] = body ptr/len; rcx, rdx, r8, r9
    # and every callee-saved register stay untouched, flags are dead at a function entry
    def build(va):
        # pushes take 7 bytes, so the lea below ends at va + 14: r11 = first slot
        code = p.asm(f"""
            push rax
            push rsi
            push rdi
            push r10
            push r11
            lea r11, [rip + {slot[0] - (va + 14):#x}]
            cmp r9, 6
            jne try7
            mov eax, dword ptr [r8]
            movzx r10d, word ptr [r8 + 4]
            cmp eax, 0x52535a41
            jne try_end
            cmp r10d, 0x4e55
            je copy
            jmp done
        try_end:
            cmp eax, 0x45535a41
            jne done
            cmp r10d, 0x444e
            jne done
            add r11, {SLOT}
            jmp copy
        try7:
            cmp r9, 7
            jne done
            cmp dword ptr [r8], 0x5a535a41
            jne done
            cmp word ptr [r8 + 4], 0x4e4f
            jne done
            cmp byte ptr [r8 + 6], 0x45
            jne done
            add r11, {2 * SLOT}
        copy:
            mov rsi, qword ptr [rsp + 0x50]
            mov r10, qword ptr [rsp + 0x58]
            cmp r10, {BODY}
            jbe fits
            mov r10d, {BODY}
        fits:
            inc dword ptr [r11]
            mov dword ptr [r11 + 4], r10d
            lea rdi, [r11 + 8]
        next:
            test r10, r10
            jz copied
            mov al, byte ptr [rsi]
            mov byte ptr [rdi], al
            inc rsi
            inc rdi
            dec r10
            jmp next
        copied:
            inc dword ptr [r11]
        done:
            pop r11
            pop r10
            pop rdi
            pop rsi
            pop rax
            push rbp
            push r15
            push r14
            push rsi
            push rdi
            push rbx
            jmp {entry + len(PROLOGUE):#x}
        """, va)
        p.check(code[7:10] == bytes.fromhex("4c8d1d"), "pvp feed cave: unexpected encoding")
        return code

    cave = p.add_cave(build)
    p.write(entry, p.asm(f"jmp {cave:#x}", entry).ljust(len(PROLOGUE), b"\x90"), expect=PROLOGUE)
    p.meta.update(rva=feed - p.ib, size=len(MAGIC) + SLOT * len(SLOTS), magic=MAGIC.decode(),
                  slots=SLOTS, slot_size=SLOT)
