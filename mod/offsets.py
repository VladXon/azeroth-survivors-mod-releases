"""Offline, evidence-backed x64 address recovery. Never opens a process.

Addresses at this API are preferred-image VAs; reports use RVAs. File-backed
reads deliberately reject virtual tails instead of pretending to read a heap.
"""
import bisect
import hashlib
import json
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import capstone
from capstone.x86_const import X86_OP_IMM, X86_OP_MEM, X86_REG_RIP
import pefile


class PatchError(Exception):
    """Cannot prove an address is suitable for this build."""


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    raw: int
    size: int
    virtual_size: int
    flags: int


def aob(pattern):
    """Compile IDA-style bytes, ??/? wildcards and nibble masks (4?, ?F)."""
    tokens = pattern.split()
    if not tokens or not any(c != '?' for t in tokens for c in t):
        raise PatchError('AOB: empty or all-wildcard pattern')
    out = []
    for token in tokens:
        if token == '?':
            token = '??'
        if not re.fullmatch(r'[0-9a-fA-F?]{2}', token):
            raise PatchError(f'AOB: invalid token {token!r}')
        if token == '??':
            out.append(b'.')
        elif '?' not in token:
            out.append(re.escape(bytes.fromhex(token)))
        else:
            values = [n for n in range(256) if all(
                c == '?' or int(c, 16) == ((n >> (4 * (1 - i))) & 15)
                for i, c in enumerate(token))]
            out.append(b'[' + re.escape(bytes(values)) + b']')
    # Lookahead retains overlapping matches.
    return re.compile(b'(?=(' + b''.join(out) + b'))', re.S)


def pointer_chain(read, root, offsets=(), *, final_offset=0, max_depth=64):
    """Return *(*root + offsets[0]) ... + final_offset, with exact u64 reads.

    The root is an address of a pointer. Each offsets element adds BEFORE the
    next dereference; final_offset adds WITHOUT dereferencing. A live reader
    may be supplied by a caller, but this module never attaches to processes.
    """
    steps = tuple(offsets)
    if len(steps) + 1 > max_depth or max_depth > 64 or max_depth < 1:
        raise PatchError('pointer chain: depth limit exceeded')

    def checked(address):
        if not isinstance(address, int) or not 0 < address < (1 << 47):
            raise PatchError(f'pointer chain: invalid user address {address!r}')
        return address

    def dereference(address):
        checked(address)
        try:
            data = read(address, 8)
        except (OSError, ValueError, struct.error) as e:
            raise PatchError(f'pointer chain: cannot read {address:#x}: {e}') from e
        if not isinstance(data, (bytes, bytearray)) or len(data) != 8:
            raise PatchError(f'pointer chain: short read at {address:#x}')
        return checked(struct.unpack('<Q', data)[0])

    value = dereference(root)
    for offset in steps:
        value = dereference(checked(value + offset))
    return checked(value + final_offset)


class OffsetResolver:
    def __init__(self, data, pe=None):
        self.source = bytes(data)
        try:
            self.pe = pe or pefile.PE(data=self.source, fast_load=True)
        except pefile.PEFormatError as e:
            raise PatchError(f'offsets: invalid PE: {e}') from e
        oh = self.pe.OPTIONAL_HEADER
        if self.pe.FILE_HEADER.Machine != 0x8664 or oh.Magic != 0x20B:
            raise PatchError('offsets: only x64 PE32+ images are supported')
        self.base = oh.ImageBase
        self.sha256 = hashlib.sha256(self.source).hexdigest()
        self.sections = []
        for s in self.pe.sections:
            raw, size = s.PointerToRawData, s.SizeOfRawData
            virtual = s.Misc_VirtualSize or size
            if (size and (raw < oh.SizeOfHeaders or raw + size > len(self.source))) or (
                    s.VirtualAddress < oh.SizeOfHeaders or
                    s.VirtualAddress + max(size, virtual) > oh.SizeOfImage):
                raise PatchError('offsets: section outside image/file')
            self.sections.append(Section(s.Name.rstrip(b'\0').decode('ascii', errors='replace'),
                                         s.VirtualAddress, raw, size, virtual, s.Characteristics))
        for attr, end in [('rva', lambda s: s.rva + max(s.size, s.virtual_size)),
                          ('raw', lambda s: s.raw + s.size)]:
            ordered = sorted((s for s in self.sections if s.size or attr == 'rva'),
                             key=lambda s: getattr(s, attr))
            if any(end(a) > getattr(b, attr) for a, b in zip(ordered, ordered[1:])):
                raise PatchError(f'offsets: overlapping {attr} mappings')
        self.md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
        self.md.detail = True
        self.symbols, self.schemas, self.references, self.chains = {}, {}, [], {}
        self._reference_keys = set()
        self._functions = None
        self._rtti = None

    def section(self, va, size=1, *, file_backed=True):
        if not isinstance(size, int) or size < 1:
            raise PatchError('offsets: read size must be positive')
        rva = va - self.base
        matches = [s for s in self.sections if s.rva <= rva and
                   rva + size <= s.rva + (min(s.size, s.virtual_size) if file_backed else s.virtual_size)]
        if len(matches) != 1:
            raise PatchError(f'offsets: {va:#x}+{size:#x} is not uniquely ' +
                             ('file-backed' if file_backed else 'mapped'))
        return matches[0]

    def file_offset(self, va, size=1):
        s = self.section(va, size)
        return s.raw + va - self.base - s.rva

    def va(self, off, size=1):
        matches = [s for s in self.sections if s.raw <= off and
                   off + size <= s.raw + min(s.size, s.virtual_size)]
        if size < 1 or len(matches) != 1:
            raise PatchError(f'offsets: file offset {off:#x} is not uniquely mapped')
        s = matches[0]
        return self.base + s.rva + off - s.raw

    def read(self, va, size):
        at = self.file_offset(va, size)
        return self.source[at:at + size]

    def scan(self, pattern, section='.text', *, limit=4096):
        rx = aob(pattern) if isinstance(pattern, str) else pattern
        if not hasattr(rx, 'finditer'):
            raise PatchError('offsets: expected AOB string or compiled bytes regex')
        selected = [s for s in self.sections if section is None or s.name == section]
        if not selected:
            raise PatchError(f'offsets: section {section!r} not found')
        hits = []
        for s in selected:
            for m in rx.finditer(self.source, s.raw, s.raw + min(s.size, s.virtual_size)):
                hits.append(self.base + s.rva + m.start() - s.raw)
                if len(hits) > limit:
                    raise PatchError('offsets: signature candidate limit exceeded')
        return hits

    def find_unique(self, name, patterns, *, section='.text', validator=None):
        hits = set()
        for pattern in patterns:
            for va in self.scan(pattern, section):
                if validator is None or validator(va):
                    hits.add(va)
        if len(hits) != 1:
            raise PatchError(f'{name}: {len(hits)} validated signature hits instead of 1')
        va = hits.pop()
        self.symbol(name, va, kind='code' if self.section(va).flags & 0x20000000 else 'data',
                    evidence='section-bounded signature and caller structural checks')
        return va

    def instruction(self, va):
        s = self.section(va)
        if not s.flags & 0x20000000:
            raise PatchError(f'offsets: instruction {va:#x} is outside executable code')
        size = min(15, self.base + s.rva + min(s.size, s.virtual_size) - va)
        insn = next(self.md.disasm(self.read(va, size), va, count=1), None)
        if insn is None:
            raise PatchError(f'offsets: invalid instruction at {va:#x}')
        return insn

    def _reference(self, insn):
        mem = [o for o in insn.operands if o.type == X86_OP_MEM and o.mem.base == X86_REG_RIP]
        if len(mem) == 1 and insn.disp_size == 4:
            return insn.address + insn.size + mem[0].mem.disp, insn.disp_offset, 'rip'
        if (insn.group(capstone.CS_GRP_CALL) or insn.group(capstone.CS_GRP_JUMP)) and (
                len(insn.operands) == 1 and insn.operands[0].type == X86_OP_IMM and insn.imm_size == 4):
            return insn.operands[0].imm, insn.imm_offset, 'rel32'
        raise PatchError(f'offsets: {insn.address:#x} is not RIP-relative or a direct rel32 branch')

    def resolve_reference(self, va, *, name=None, record=True):
        insn = self.instruction(va)
        target, _, kind = self._reference(insn)
        section = self.section(target, file_backed=False)
        if kind == 'rel32' and not section.flags & 0x20000000:
            raise PatchError(f'offsets: branch {va:#x} does not target executable code')
        key = (va, target, kind)
        if record and key not in self._reference_keys:
            self.references.append({'instruction_rva': va - self.base, 'target_rva': target - self.base,
                                    'kind': kind, 'bytes': bytes(insn.bytes).hex()})
            self._reference_keys.add(key)
        if name:
            self.symbol(name, target, kind='code' if kind == 'rel32' else 'data',
                        evidence=f'{kind} at RVA {va - self.base:#x}')
        return target

    def resolve_capture(self, displacement_off, size=4, *, record=True):
        """Validate a regex's disp32 capture ending at the instruction end."""
        if size != 4:
            raise PatchError('offsets: reference capture must be disp32')
        end = self.va(displacement_off, size) + size
        candidates = []
        for length in range(5, 16):
            start = end - length
            try:
                insn = self.instruction(start)
                _, disp, _ = self._reference(insn)
                if start + insn.size == end and start + disp == end - 4:
                    candidates.append(start)
            except PatchError:
                continue
        if not candidates:
            raise PatchError(f'offsets: no reference instruction for capture at {displacement_off:#x}')
        # Include the REX prefix when both prefixless and full decodes are possible.
        return self.resolve_reference(min(candidates), record=record)

    def symbol(self, name, va, *, kind='code', evidence):
        s = self.section(va, file_backed=False)
        if kind == 'code' and not s.flags & 0x20000000:
            raise PatchError(f'{name}: expected executable address')
        old = self.symbols.get(name)
        if old and (old['rva'] != va - self.base or old['kind'] != kind):
            raise PatchError(f'{name}: conflicting addresses')
        entry = self.symbols.setdefault(name, {'rva': va - self.base, 'kind': kind,
                                               'section': s.name, 'evidence': []})
        if evidence not in entry['evidence']:
            entry['evidence'].append(evidence)
        return va

    def follow_pointer(self, name, root, offsets=(), *, final_offset=0):
        steps = tuple(offsets)
        target = pointer_chain(self.read, root, steps, final_offset=final_offset)
        self.section(target)  # only file-backed terminal objects in offline recovery
        record = {'root_rva': root - self.base, 'offsets': list(steps),
                  'final_offset': final_offset, 'target_rva': target - self.base,
                  'source': 'file-backed pointer chain'}
        if name in self.chains and self.chains[name] != record:
            raise PatchError(f'{name}: contradictory pointer chain')
        self.chains[name] = record
        return self.symbol(name, target, kind='data', evidence=f'pointer chain from RVA {root - self.base:#x}')

    def field(self, schema, name, instructions, *, base, width, index=None):
        values, evidence = set(), []
        for va in instructions:
            insn = self.instruction(va)
            mem = [o for o in insn.operands if o.type == X86_OP_MEM and
                   insn.reg_name(o.mem.base) == base and o.size == width and
                   (insn.reg_name(o.mem.index) if o.mem.index else None) == index and
                   (index is None or o.mem.scale == 1)]
            if len(mem) != 1 or not 0 <= mem[0].mem.disp < 0x100000:
                raise PatchError(f'{schema}.{name}: unexpected memory operand at {va:#x}')
            values.add(mem[0].mem.disp)
            evidence.append({'instruction_rva': va - self.base, 'bytes': bytes(insn.bytes).hex(),
                             'instruction': f'{insn.mnemonic} {insn.op_str}'})
        if len(values) != 1:
            raise PatchError(f'{schema}.{name}: conflicting or missing field evidence {values}')
        offset = values.pop()
        fields = self.schemas.setdefault(schema, {})
        old = fields.get(name)
        if old and (old['offset'], old['size']) != (offset, width):
            raise PatchError(f'{schema}.{name}: conflicting field definitions')
        entry = fields.setdefault(name, {'offset': offset, 'size': width,
                                         'source': 'instruction-derived', 'evidence': []})
        for item in evidence:
            if item not in entry['evidence']:
                entry['evidence'].append(item)
        return offset

    def function_range(self, va):
        if self._functions is None:
            directory = self.pe.OPTIONAL_HEADER.DATA_DIRECTORY[3]
            if not directory.VirtualAddress or not directory.Size or directory.Size % 12:
                raise PatchError('offsets: invalid or missing exception directory')
            raw = self.read(self.base + directory.VirtualAddress, directory.Size)
            functions = [(b, e) for b, e, _ in struct.iter_unpack('<III', raw) if b or e]
            if not functions or any(b >= e for b, e in functions) or functions != sorted(functions):
                raise PatchError('offsets: invalid runtime function table')
            if any(e > nb for (_, e), (nb, _) in zip(functions, functions[1:])):
                raise PatchError('offsets: overlapping runtime functions')
            self._functions = functions
            self._starts = [b for b, _ in functions]
        index = bisect.bisect_right(self._starts, va - self.base) - 1
        if index < 0 or not self._functions[index][0] <= va - self.base < self._functions[index][1]:
            raise PatchError(f'offsets: no runtime function contains {va:#x}')
        begin, end = self._functions[index]
        self.section(self.base + begin, end - begin)
        return self.base + begin, self.base + end

    def _rtti_at(self, locator):
        sig, offset, cd_offset, td, chd, own = struct.unpack('<6I', self.read(locator, 24))
        if sig != 1 or own != locator - self.base:
            raise PatchError('RTTI: invalid complete object locator')
        descriptor = self.base + td
        type_vtable, spare = struct.unpack('<QQ', self.read(descriptor, 16))
        self.section(type_vtable, 8)
        if spare:
            raise PatchError('RTTI: unexpected TypeDescriptor spare')
        section = self.section(descriptor + 16)
        size = min(1024, self.base + section.rva + min(section.size, section.virtual_size) - descriptor - 16)
        name = self.read(descriptor + 16, size).split(b'\0', 1)[0]
        if len(name) == size or not re.fullmatch(rb'\.\?A[UV][!-~]+@@', name):
            raise PatchError('RTTI: invalid decorated type name')
        signature, attrs, count, array = struct.unpack('<4I', self.read(self.base + chd, 16))
        if signature != 0 or attrs & ~7 or not 1 <= count <= 256:
            raise PatchError('RTTI: invalid class hierarchy')
        bases = struct.unpack(f'<{count}I', self.read(self.base + array, 4 * count))
        for i, entry in enumerate(bases):
            btd, contained, mdisp, pdisp, vdisp, flags = struct.unpack('<IIiiiI', self.read(self.base + entry, 24))
            self.section(self.base + btd, 17)
            if (i == 0 and btd != td) or contained >= count or flags & ~0x7F:
                raise PatchError('RTTI: invalid base class descriptor')
        return {'name': name.decode('ascii'), 'locator_rva': own, 'type_descriptor_rva': td,
                'subobject_offset': offset, 'constructor_displacement': cd_offset,
                'base_count': count}

    def discover_rtti(self, *, max_candidates=4096, max_methods=256):
        """MSVC x64 ABI only. Validated table candidates are never patch fallbacks.

        Rust trait-object tables and object addresses require separate evidence.
        Method enumeration is bounded; `methods_truncated` marks the bound.
        """
        if self._rtti is not None:
            return self._rtti
        classes, locators = [], {}
        candidates = 0
        for s in self.sections:
            if s.flags & 0x20000000 or not s.flags & 0x40000000 or s.flags & 0x80000000:
                continue
            end = s.raw + min(s.size, s.virtual_size)
            for m in re.finditer(rb'\x01\x00\x00\x00', self.source[s.raw:end]):
                at = s.raw + m.start()
                if at % 4 or at + 24 > end:
                    continue
                va = self.base + s.rva + at - s.raw
                if struct.unpack_from('<I', self.source, at + 20)[0] != va - self.base:
                    continue
                candidates += 1
                if candidates > max_candidates:
                    raise PatchError('RTTI: candidate limit exceeded')
                try:
                    locators[va] = self._rtti_at(va)
                except PatchError:
                    continue
        for locator, info in locators.items():
            needle = struct.pack('<Q', locator)
            for va in self.scan(re.compile(re.escape(needle)), section=None):
                s = self.section(va, 16)
                if va % 8 or s.flags & (0x20000000 | 0x80000000):
                    continue
                methods = []
                for i in range(max_methods + 1):
                    try:
                        target = struct.unpack('<Q', self.read(va + 8 + i * 8, 8))[0]
                        if not self.section(target).flags & 0x20000000:
                            break
                        methods.append(target - self.base)
                    except PatchError:
                        break
                if methods:
                    classes.append(dict(info, vtable_rva=va + 8 - self.base,
                                        methods=methods[:max_methods], methods_truncated=len(methods) > max_methods,
                                        status='validated-static-candidate'))
        self._rtti = classes
        return classes

    def identify_vtable(self, va):
        hits = [r for r in self.discover_rtti() if r['vtable_rva'] == va - self.base]
        if len(hits) != 1:
            raise PatchError(f'RTTI: unknown/ambiguous vtable {va:#x}')
        return hits[0]

    def report(self):
        rtti = self.discover_rtti()
        return {'format_version': 1, 'source_sha256': self.sha256, 'source_size': len(self.source),
                'machine': 'x86_64', 'preferred_image_base': self.base, 'address_format': 'rva',
                'symbols': self.symbols, 'schemas': self.schemas, 'references': self.references,
                'pointer_chains': self.chains,
                'rtti': rtti, 'capabilities': {
                    'aob': 'available', 'rip_relative': 'available',
                    'pointer_chains': 'file-backed-only; no live process attached',
                    'schema': 'instruction-derived; native reflection not established',
                    'rtti': 'validated-static-candidates' if rtti else 'no validated MSVC x64 tables',
                    'automatic_dump': 'available'}}

    def dump(self, path, **extra):
        report = self.report()
        if set(extra) & set(report):
            raise PatchError('offsets: report identity cannot be overridden')
        report.update(extra)
        path = Path(path)
        temp = path.with_suffix(path.suffix + '.tmp')
        temp.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        temp.replace(path)


def validate_report(report, source):
    """Reject stale reports. Reports are diagnostics, never an executable cache."""
    if (not isinstance(report, dict) or report.get('format_version') != 1 or report.get('source_size') != len(source) or
            report.get('source_sha256') != hashlib.sha256(source).hexdigest()):
        raise PatchError('offsets: report does not belong to these executable bytes')
