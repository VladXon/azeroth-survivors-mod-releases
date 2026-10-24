"""Azeroth Survivors: 52 talent points, plus the extra patches in patches/.

Builds AzerothSurvivors-52.exe next to the official exe. Patch sites are found by
byte signatures, not fixed addresses, so after an official update the copy is
simply rebuilt from the new exe. The official exe itself is never modified, so
the game's own updater keeps working on it (it ignores exes with another name).
"""
import hashlib
import importlib.util
import json
import re
import struct
import sys
from pathlib import Path

import capstone
import keystone
import pefile

from offsets import OffsetResolver, PatchError, validate_report

HERE = Path(__file__).resolve().parent
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]  # launcher flags like --pvp aren't paths
SRC = Path(ARGS[0]) if ARGS else HERE.parent / "AzerothSurvivors.exe"
DST = SRC.with_name("AzerothSurvivors-52.exe")
STAMP = DST.with_suffix(".source-sha256")
OLD_MAX, NEW_MAX = 51, 52

md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)


CAVE_NAME = b".modtxt\0"
CAVE_SIZE = 0x8000  # room for all patches/*.py caves; .moddata follows it
DATA_NAME = b".moddata"
ks = keystone.Ks(keystone.KS_ARCH_X86, keystone.KS_MODE_64)


def align(x, a):
    return (x + a - 1) // a * a


class Patcher:
    def __init__(self, data):
        self.d = bytearray(data)
        self.pe = pefile.PE(data=bytes(data), fast_load=True)
        self.ib = self.pe.OPTIONAL_HEADER.ImageBase
        self.offsets = OffsetResolver(data, pe=self.pe)
        self.log = []
        oh = self.pe.OPTIONAL_HEADER
        last = max(self.pe.sections, key=lambda s: s.VirtualAddress)
        self.cave_va = self.ib + align(last.VirtualAddress + last.Misc_VirtualSize, oh.SectionAlignment)
        self.cave = bytearray()
        # .moddata follows .modtxt, whose virtual size is always CAVE_SIZE, so its va is known up front
        self.data_va = self.cave_va + align(CAVE_SIZE, oh.SectionAlignment)
        self.data = bytearray()
        self.relocs = []  # vas of absolute pointers we wrote, rebased by the loader (ASLR)
        self.meta = {}  # written next to the built exe as <name>.feed.json when not empty

    def check(self, ok, message):
        """For patches/*.py: fail the whole build instead of writing a half-patched exe."""
        check(ok, message)

    def asm(self, code, va):
        """Assembles x86-64 code (Intel syntax) as if placed at va."""
        return bytes(ks.asm(code, va)[0])

    def add_cave(self, build):
        """Places code in the mod's own executable section. build(va) returns the bytes for
        that address (so rip-relative and rel32 jumps resolve). Returns va."""
        va = self.cave_va + len(self.cave)
        code = build(va)
        self.cave += code + b"\xcc" * (-len(code) % 16)
        check(len(self.cave) <= CAVE_SIZE, "code cave overflow")
        return va

    def add_data(self, size, init=b""):
        """Reserves size bytes of read-write memory in the mod's own data section, starting
        with init. Returns va; cave code reaches it rip-relative."""
        check(len(init) <= size, "add_data: init is larger than size")
        va = self.data_va + len(self.data)
        self.data += init + b"\0" * (size - len(init))
        self.data += b"\0" * (-len(self.data) % 16)
        return va

    def add_reloc(self, va):
        """Marks a u64 absolute pointer at va (e.g. in add_data memory) for base relocation."""
        self.relocs.append(va)

    def _move_relocs(self):
        """The reloc directory must be one table, so finish() copies the original one plus
        blocks for our pointers to the end of .moddata and repoints the data directory."""
        oh = self.pe.OPTIONAL_HEADER
        rdir = oh.DATA_DIRECTORY[5]
        o = self.off(self.ib + rdir.VirtualAddress)
        table = bytearray(self.d[o:o + rdir.Size])
        pages = {}
        for va in sorted(set(self.relocs)):
            rva = va - self.ib
            pages.setdefault(rva & ~0xFFF, []).append(0xA000 | (rva & 0xFFF))  # IMAGE_REL_BASED_DIR64
        for page, entries in pages.items():
            if len(entries) % 2:
                entries.append(0)  # IMAGE_REL_BASED_ABSOLUTE pads the block to 4 bytes
            table += struct.pack("<II", page, 8 + 2 * len(entries)) + struct.pack(f"<{len(entries)}H", *entries)
        va = self.add_data(len(table), bytes(table))
        dd = oh.get_file_offset() + 112 + 5 * 8  # PE32+ DataDirectory[BASERELOC]
        struct.pack_into("<II", self.d, dd, va - self.ib, len(table))
        self.log.append(f"base relocations: +{len(set(self.relocs))} pointers, table moved to {va:x}")

    def write(self, va, code, expect=None):
        """Overwrites bytes at va; expect guards against patching the wrong code."""
        o = self.off(va)
        if expect is not None:
            check(bytes(self.d[o:o + len(expect)]) == expect, f"{va:x}: unexpected bytes")
        self.d[o:o + len(code)] = code
        self.log.append(f"{va:x}  {len(code)} bytes: {code.hex()}")

    def finish(self):
        """Appends .modtxt (RX) and .moddata (RW) if patches used them. .modtxt is emitted
        whenever .moddata is, so the sections stay contiguous in memory."""
        if self.relocs:
            self._move_relocs()
        new = []
        if self.cave or self.data:
            new.append((CAVE_NAME, self.cave, CAVE_SIZE, self.cave_va, 0x60000020))
        if self.data:
            new.append((DATA_NAME, self.data, len(self.data), self.data_va, 0xC0000040))
        if not new:
            return
        oh, fh = self.pe.OPTIONAL_HEADER, self.pe.FILE_HEADER
        table = self.pe.DOS_HEADER.e_lfanew + 24 + fh.SizeOfOptionalHeader
        n = fh.NumberOfSections
        check(table + 40 * (n + len(new)) <= oh.SizeOfHeaders, "no room for the section headers")
        check(not any(self.d[table + 40 * n:table + 40 * (n + len(new))]), "section table slack is not empty")
        last = max(self.pe.sections, key=lambda s: s.PointerToRawData)
        check(last.PointerToRawData + last.SizeOfRawData == len(self.d), "file has an overlay")
        for k, (name, body, vsize, va, flags) in enumerate(new):
            raw = align(len(self.d), oh.FileAlignment)
            size = align(len(body), oh.FileAlignment)
            self.d += b"\0" * (raw - len(self.d)) + body + b"\0" * (size - len(body))
            at = table + 40 * (n + k)
            self.d[at:at + 40] = name + struct.pack("<IIIIIIHHI", vsize, va - self.ib, size,
                                                    raw if size else 0, 0, 0, 0, 0, flags)
        struct.pack_into("<H", self.d, self.pe.DOS_HEADER.e_lfanew + 6, n + len(new))
        name, body, vsize, va, flags = new[-1]
        struct.pack_into("<I", self.d, oh.get_file_offset() + 56, align(va - self.ib + vsize, oh.SectionAlignment))

    def sec(self, name):
        return next(s for s in self.pe.sections if s.Name.rstrip(b"\0") == name)

    def va(self, off):
        return self.ib + self.pe.get_rva_from_offset(off)

    def off(self, va):
        return self.pe.get_offset_from_rva(va - self.ib)

    def insns(self, off, count):
        return list(md.disasm(bytes(self.d[off:off + count * 15]), self.va(off), count))

    def put_imm(self, insn, old, new):
        """Rewrites the immediate at the end of an instruction: imm8 for opcode 0x83, else imm32."""
        opcode = insn.bytes[1] if 0x40 <= insn.bytes[0] <= 0x4F else insn.bytes[0]
        fmt = "<B" if opcode == 0x83 else "<I"
        at = self.off(insn.address) + insn.size - struct.calcsize(fmt)
        cur = struct.unpack_from(fmt, self.d, at)[0]
        assert cur == old, f"{insn.address:x}: imm {cur} != {old}"
        struct.pack_into(fmt, self.d, at, new)
        self.log.append(f"{insn.address:x}  {insn.mnemonic} {insn.op_str}  ->  {new:#x}")


def reg_of(op_str):
    return op_str.split(",")[0].strip()


def mov_imm_bytes(reg, imm):
    """Encoding of `mov r32, imm32`."""
    regs = ["eax", "ecx", "edx", "ebx", "esp", "ebp", "esi", "edi"]
    if reg in regs:
        return bytes([0xB8 + regs.index(reg)]) + imm.to_bytes(4, "little")
    return bytes([0x41, 0xB8 + int(reg[1:-1]) - 8]) + imm.to_bytes(4, "little")


def patch_talent_curve(p):
    """Each talent-points loop pays 3 thresholds per cycle for 17 cycles (51 points).
    Starting every class at 1 free point and capping at 52 keeps all thresholds as is."""
    text = p.sec(b".text")
    lo, hi = text.PointerToRawData, text.PointerToRawData + text.SizeOfRawData
    body = re.compile(rb"\x8d[\x40-\x7f]\xe2.{2,14}?\x8d[\x40-\x7f]\xf1", re.S)
    loops = points = counters = cmps = 0
    for m in body.finditer(p.d, lo, hi):
        loop, start, j = None, None, None
        for start in (m.start() - 1, m.start()):  # with or without a REX prefix on the lea
            loop = p.insns(start, 24)
            j = next((i for i in loop if i.mnemonic == "jne" and i.op_str == hex(p.va(start))), None)
            if j:
                break
        check(j, f"curve-like code at {p.va(m.start()):x} is not a recognised loop")
        loops += 1
        inside = [i for i in loop if i.address <= j.address]
        after = p.insns(p.off(j.address + j.size), 4)
        last_cmp = inside[-2]
        if last_cmp.mnemonic == "cmp" and last_cmp.op_str.endswith(", 0x33"):
            patch_counter_loop(p, start, last_cmp, after)
            counters += 1
            continue
        adds3 = {reg_of(i.op_str) for i in inside if i.mnemonic == "add" and i.op_str.endswith(", 3")}
        cap = next((i for i in after if i.mnemonic == "mov" and i.op_str.endswith(", 0x33")
                    and reg_of(i.op_str) in adds3), None)
        if cap is None:
            # progress-only loop (xp into the next point) has no `add reg, 3` at all
            check(not adds3, f"loop at {p.va(start):x}: points cap not found")
            continue
        reg = reg_of(cap.op_str)
        init = p.d.rfind(mov_imm_bytes(reg, 2), start - 0x40, start)
        check(init > 0 and decodes_into(p, init, start), f"init of {reg} not found before {p.va(start):x}")
        p.put_imm(p.insns(init, 1)[0], 2, 3)
        p.put_imm(cap, OLD_MAX, NEW_MAX)
        points += 1
        # achievement checks right after the loop: cmp reg, 51 / setae
        nxt = after[after.index(cap) + 1]
        merge = p.off(int(nxt.op_str, 16)) if nxt.mnemonic == "jmp" else p.off(nxt.address)
        for i in p.insns(merge, 3):
            if i.mnemonic == "cmp" and i.op_str == f"{reg}, 0x33":
                p.put_imm(i, OLD_MAX, NEW_MAX)
                cmps += 1
    return loops, points, counters, cmps


def patch_counter_loop(p, start, last_cmp, after):
    """Variant where [mem] += 3 per cycle and the loop ends when it reaches 51.
    Both stores of 0 into [mem] (before the loop, and the 'class not found' path
    before the merge point) become 1."""
    store, nxt = after[0], after[1]
    check(store.mnemonic == "mov" and store.op_str.endswith(", 0x33") and nxt.mnemonic == "jmp",
          f"counter loop at {p.va(start):x} has an unknown exit")
    p.put_imm(last_cmp, OLD_MAX, NEW_MAX)
    p.put_imm(store, OLD_MAX, NEW_MAX)
    zero = bytes(store.bytes[:-4]) + b"\0\0\0\0"
    init = p.d.rfind(zero, start - 0x40, start)
    check(init > 0 and decodes_into(p, init, start), f"counter init not found before {p.va(start):x}")
    p.put_imm(p.insns(init, 1)[0], 0, 1)
    merge = int(nxt.op_str, 16)
    o = p.off(nxt.address + nxt.size)
    for i in p.insns(o, 64):
        if i.address >= merge:
            break
        if bytes(i.bytes) == zero:
            p.put_imm(i, 0, 1)


def decodes_into(p, off, target):
    """True when linear decoding from off lands exactly on target (the match is a real instruction)."""
    for i in p.insns(off, 32):
        if p.off(i.address) >= target:
            return p.off(i.address) == target
    return False


def check(ok, message):
    if not ok:
        raise PatchError(message)


def patch_max_constants(p):
    """u32 51 statics passed as {max}/{n} to the talent UI and achievement texts."""
    sigs = [
        rb"points\x00\x00(3\x00\x00\x00)maxxptotalbefore",
        rb"(3\x00\x00\x00)picker\.points_of",
        rb"talent points with one class\.n\x00\x00(3\x00\x00\x00)",
    ]
    for sig in sigs:
        hits = [m.start(1) for m in re.finditer(sig, p.d)]
        check(len(hits) == 1, f"{sig!r}: {len(hits)} hits instead of 1")
        struct.pack_into("<I", p.d, hits[0], NEW_MAX)
        p.log.append(f"{p.va(hits[0]):x}  const 51 -> 52  {sig!r}")


def build_digest(data):
    modules = sorted((HERE / "patches").glob("*.py"))
    scripts = sorted((HERE / "patches").glob("*.lua"))  # Lua the patches embed (quest.lua)
    return hashlib.sha256(b"".join([data, Path(__file__).read_bytes(),
                                  (HERE / "offsets.py").read_bytes()]
                                 + [m.read_bytes() for m in modules + scripts])).hexdigest()


PATCH_STAGES = {
    'coop_choice': 'Защита выбора награды',
    'coop_choice_cast': 'Эффекты выбора награды',
    'coop_choice_push': 'Отталкивание после выбора',
    'coop_ghost': 'Кооператив: поиск адресов и сборка',
    'coop_shared': 'Общие враги',
    'coop_spawns': 'Появление врагов',
    'custom_jewels': 'Дополнительные кольца',
    'custom_jewels_bans': 'Запреты колец',
    'custom_jewels_icons': 'Иконки колец',
    'global_talents': 'Общий прогресс талантов',
    'global_zones': 'Доступные зоны',
    'magnet_homing': 'Подбор опыта',
    'pvp_feed': 'Данные онлайн-матча',
    'quest': 'Квесты: поиск адресов и сборка',
}


def apply_patches(p, progress=None):
    """Resolve and patch in memory. Used by the builder and the offline dumper."""
    progress = progress or (lambda message: None)
    progress('Таланты: поиск адресов и сборка…')
    loops, points, counters, cmps = patch_talent_curve(p)
    check(points + counters > 0, "talent curve not found")
    patch_max_constants(p)
    for m in sorted((HERE / "patches").glob("*.py")):
        progress(PATCH_STAGES.get(m.stem, 'Дополнительные патчи') + '…')
        spec = importlib.util.spec_from_file_location(m.stem, m)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        p.log.append(f"--- {m.stem}")
        module.patch(p)
    progress('Подготовка нового EXE…')
    p.finish()
    p.log.append(f"curve: {loops} loops, {points} point counters, {counters} counter loops, {cmps} checks")


def build_report(p, digest):
    return dict(p.offsets.report(), build_digest=digest,
                patched_sha256=hashlib.sha256(p.d).hexdigest(),
                patch_metadata=p.meta,
                disabled_features=[line for line in p.log if "disabled" in line],
                status='resolved')


def is_current(data, digest, dst, stamp):
    if not dst.exists() or not stamp.exists() or stamp.read_text().strip() != digest:
        return False
    try:
        report = json.loads(dst.with_suffix('.offsets.json').read_text(encoding='utf-8'))
        validate_report(report, data)
        metadata = report.get('patch_metadata')
        if metadata and json.loads(dst.with_suffix('.feed.json').read_text(encoding='utf-8')) != metadata:
            return False
        return (report.get('build_digest') == digest and report.get('status') == 'resolved' and
                report.get('patched_sha256') == hashlib.sha256(dst.read_bytes()).hexdigest())
    except (OSError, ValueError, PatchError):
        return False


def main(progress=None):
    progress = progress or (lambda message: None)
    progress('Чтение игрового файла…')
    data = SRC.read_bytes()
    progress('Проверка актуальности мода…')
    digest = build_digest(data)
    if is_current(data, digest, DST, STAMP):
        print("up to date")
        progress('Мод актуален')
        return 0
    try:
        progress('Проверка структуры игры…')
        p = Patcher(data)
        apply_patches(p, progress=progress)
        progress('Проверка адресов и результата сборки…')
        report = build_report(p, digest)  # complete validation before replacing any output
    except (PatchError, AssertionError) as e:
        print(f"the game changed, patch not applied: {e}")
        return 1
    progress('Сохранение собранного мода…')
    tmp = DST.with_suffix(".tmp")
    tmp.write_bytes(p.d)
    feed = DST.with_suffix(".feed.json")
    report_path = DST.with_suffix('.offsets.json')
    report_tmp = report_path.with_suffix('.json.tmp')
    report_tmp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    feed_tmp = feed.with_suffix('.json.tmp')
    if p.meta:
        feed_tmp.write_text(json.dumps(p.meta, indent=2), encoding='utf-8')
    tmp.replace(DST)
    report_tmp.replace(report_path)
    if p.meta:
        feed_tmp.replace(feed)
    else:
        feed.unlink(missing_ok=True)
    STAMP.write_text(digest)
    print("\n".join(p.log))
    print(f"built {DST.name}")
    print(f"offsets: {report_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
