"""Reads and writes the game's sealed save (%APPDATA%\\wenilla-rogue\\characters.txt).

Format: "RSE1:" + base64(iv[16] + ciphertext + tag[20]). The keystream is
HMAC-SHA1(key, "ks" || iv || block_index u64le), 20 bytes per block; the tag is
HMAC-SHA1(key, "tag" || iv || ciphertext). The key is read from the exe, where it
sits XORed with 0x36 as the HMAC inner pad.
Run: python save.py dump > save.txt   /   python save.py load save.txt
"""
import base64
import hashlib
import hmac
import os
import struct
import sys
from pathlib import Path

GAME = Path(__file__).resolve().parent.parent
# WOW_ROGUE_SAVE is the save file itself; the game keeps Saves\, backups and lang\ next to it
SAVE = Path(os.environ.get("WOW_ROGUE_SAVE") or Path(os.environ["APPDATA"]) / "wenilla-rogue" / "characters.txt")
PVP_SAVE = Path(os.environ["APPDATA"]) / "wenilla-rogue" / "pvp" / "characters.txt"
KEY_PREFIX = b"azeroth-survivors/save/v1/"


def read_key(exe=GAME / "AzerothSurvivors.exe"):
    data = exe.read_bytes()
    at = data.find(bytes(c ^ 0x36 for c in KEY_PREFIX))
    if at < 0:
        raise ValueError("save key not found in the exe")
    # the exe holds the first 48 bytes of the pad here; the rest of the 64-byte block is padding
    return bytes(c ^ 0x36 for c in data[at:at + 48]).rstrip(b"\0")


def _mac(key, *parts):
    return hmac.new(key, b"".join(parts), hashlib.sha1).digest()


def _xor(key, iv, data):
    out = bytearray(data)
    for i in range(0, len(out), 20):
        ks = _mac(key, b"ks", iv, struct.pack("<Q", i // 20))
        for j in range(min(20, len(out) - i)):
            out[i + j] ^= ks[j]
    return bytes(out)


def load(key, path=SAVE):
    raw = base64.b64decode(path.read_bytes()[5:].strip())
    iv, ct, tag = raw[:16], raw[16:-20], raw[-20:]
    if not hmac.compare_digest(_mac(key, b"tag", iv, ct), tag):
        raise ValueError(f"{path}: bad tag, not a save of this game version")
    return _xor(key, iv, ct).decode()


def dump(key, text, path=SAVE):
    iv = os.urandom(16)
    ct = _xor(key, iv, text.encode())
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(b"RSE1:" + base64.b64encode(iv + ct + _mac(key, b"tag", iv, ct)))
    tmp.replace(path)


CLASSES = [1, 2, 3, 4, 5, 7, 8, 9, 11]


def _int(value):
    """int(value), or None for a malformed field (a broken entry is skipped, never fatal)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def talent_xp(lines):
    """{line index: (class, xp)} of the well-formed talent lines."""
    out = {}
    for i, l in enumerate(lines):
        parts = l.split("|")
        if parts[0] == "talent" and len(parts) >= 3 and None not in (_int(parts[1]), _int(parts[2])):
            out[i] = (_int(parts[1]), _int(parts[2]))
    return out


def share_talent_xp(text):
    """Every class gets the talent xp of the best one (spent talents stay per class).
    The exe patch keeps records in sync during play but only touches classes that
    already have a record, so every class gets one here."""
    lines = text.split("\n")
    records = talent_xp(lines)
    talent = {cls: i for i, (cls, _) in records.items()}
    if not talent:
        return text
    best = max(xp for _, xp in records.values())
    for cls in CLASSES:
        if cls in talent:
            parts = lines[talent[cls]].split("|")
            parts[2] = str(best)
            lines[talent[cls]] = "|".join(parts)
        else:
            lines.insert(max(talent.values()) + 1, f"talent|{cls}|{best}|")
            talent[cls] = max(talent.values()) + 1
    return "\n".join(lines)


ZONES = ["elwynn", "duskwood", "wsg", "ungoro", "ab", "feralas", "arena", "sm", "av", "brm", "aq", "naxx"]


def _zone_field(l):
    return l.split("|")[2] if l.count("|") >= 2 else ""


def opened_count(lines):
    """How many zones the furthest class has opened, 0 when none. The game stores
    'zones opened' per class as a count: open line = index + 1, beaten zone = index + 2."""
    field = _zone_field
    opened = [ZONES.index(field(l)) + 1 for l in lines if l.startswith("open|") and field(l) in ZONES]
    opened += [ZONES.index(z) + 2 for l in lines if l.startswith("beat|") for z in field(l).split(",") if z in ZONES]
    return min(max(opened, default=0), len(ZONES))


def share_zones(text):
    """Every class gets the zones opened by the furthest one."""
    lines = text.split("\n")
    field = _zone_field
    count = opened_count(lines)
    if not count:
        return text
    zone = ZONES[count - 1]
    # unparsable lines (unknown zone, non-numeric class) stay as they are
    shared = lambda l: l.startswith("open|") and field(l) in ZONES and _int(l.split("|")[1]) is not None
    at = next((i for i, l in enumerate(lines) if shared(l)), 1)
    lines = [l for l in lines if not shared(l)]
    lines[at:at] = [f"open|{cls}|{zone}" for cls in CLASSES]
    return "\n".join(lines)


def pvp_profile(text, zone, preset):
    """The PvP run's save: every class has exactly the drawn zone open, the run starts
    there with the match's Run Options. pick = class|race|gender|zone|name|look|options,
    options = density,health,damage,speed."""
    lines = [l for l in text.split("\n") if not l.startswith("open|")]
    at = next((i for i, l in enumerate(lines) if l.startswith("pick|")), None)
    if at is None:
        raise ValueError("the save has no hero picked yet: play one run first")
    pick = lines[at].split("|")
    pick[4] = zone
    pick[7] = ",".join(str(int(v)) for v in preset)
    lines[at] = "|".join(pick)
    lines[at + 1:at + 1] = [f"open|{cls}|{zone}" for cls in CLASSES]
    return "\n".join(lines)


def _line(lines, tag):
    """(index, fields after the tag) of the first `tag|...` line, or (None, None)."""
    for i, l in enumerate(lines):
        if l.startswith(tag + "|"):
            return i, l[len(tag) + 1:]
    return None, None


def _pairs(body):
    """'k=v,k=v' bodies of seen, stat and achat as {k: int}; malformed entries are skipped."""
    out = {}
    for p in body.split(","):
        k, _, v = p.partition("=")
        if k and _int(v) is not None:
            out[k] = _int(v)
    return out


def merge_zones(lines, end_lines):
    """Main save lines with the zone progress of a co-op profile added: per class the furthest
    opened zone and the union of beaten zones, never lowered. A co-op host can pick any zone,
    so only wins in zones this save had already opened count, each opening the next zone;
    the profile's own open lines are ignored."""
    def zone_field(l):
        parts = l.split("|")
        return (_int(parts[1]), parts[2]) if len(parts) >= 3 and _int(parts[1]) is not None else (None, "")

    def parsed(l):
        """A zone line this merge understands; anything else stays in the save untouched."""
        cls, field = zone_field(l)
        return cls is not None and ((l.startswith("open|") and field in ZONES) or l.startswith("beat|"))

    opened, beaten = {}, {}
    for l in lines:
        if not parsed(l):
            continue
        cls, field = zone_field(l)
        if l.startswith("open|"):
            opened[cls] = max(opened.get(cls, 0), ZONES.index(field))
        else:
            beaten.setdefault(cls, set()).update(z for z in field.split(",") if z in ZONES)
    count = max(1, opened_count(lines))  # the first zone is always open
    for l in end_lines:
        if not (l.startswith("beat|") and parsed(l)):
            continue
        cls, field = zone_field(l)
        won = [ZONES.index(z) for z in field.split(",") if z in ZONES and ZONES.index(z) < count]
        beaten.setdefault(cls, set()).update(ZONES[i] for i in won)
        if won:
            opened[cls] = max(opened.get(cls, 0), min(max(won) + 1, len(ZONES) - 1))
    at = next((i for i, l in enumerate(lines) if parsed(l)), 1)
    lines = [l for l in lines if not parsed(l)]
    lines[at:at] = ([f"open|{cls}|{ZONES[i]}" for cls, i in opened.items()]
                    + [f"beat|{cls}|{','.join(z for z in ZONES if z in zs)}" for cls, zs in beaten.items()])
    return lines


def merge_pvp(main, start, end, zones=False):
    """Brings what a match run earned back into the main save: talent xp gained, items,
    codex (seen), stats, achievements, and with zones=True (co-op) zone progress too.
    Picks, gear and looks stay as they are; PvP never touches zones."""
    lines = share_talent_xp(main).split("\n")  # every class has a talent line to take spent talents
    start_lines, end_lines = start.split("\n"), end.split("\n")

    # the exe equalizes every class during the run, so the gain is best-after minus best-before
    best = lambda text_lines: max([xp for _, xp in talent_xp(text_lines).values()] + [0])
    gained = max(best(end_lines) - best(start_lines), 0)
    for i in talent_xp(lines) if gained else ():
        parts = lines[i].split("|")
        parts[2] = str(int(parts[2]) + gained)
        lines[i] = "|".join(parts)

    # talents spent in the match game: a class whose spent field changed takes the profile's one,
    # unless the main save changed it too since the match began (that newer choice wins)
    spent = lambda text_lines: {cls: text_lines[i].split("|")[3:] for i, (cls, _) in talent_xp(text_lines).items()}
    before, after, now = spent(start_lines), spent(end_lines), spent(lines)
    for i, (cls, _) in talent_xp(lines).items():
        if cls in after and after[cls] != before.get(cls) and now[cls] == before.get(cls):
            lines[i] = "|".join(lines[i].split("|")[:3] + after[cls])

    def put(tag, body):
        i, _ = _line(lines, tag)
        if i is None:
            lines.append(f"{tag}|{body}")
        else:
            lines[i] = f"{tag}|{body}"

    def body(text_lines, tag):
        return _line(text_lines, tag)[1] or ""

    items = [x for x in body(lines, "items").split(",") if x]
    items += [x for x in body(end_lines, "items").split(",") if x and x not in items]
    if items:
        put("items", ",".join(items))

    seen = _pairs(body(lines, "seen"))
    for k, v in _pairs(body(end_lines, "seen")).items():
        seen[k] = max(v, seen.get(k, v))
    if seen:
        put("seen", ",".join(f"{k}={v}" for k, v in sorted(seen.items())))

    stat = _pairs(body(lines, "stat"))
    start_stat = _pairs(body(start_lines, "stat"))
    for k, v in _pairs(body(end_lines, "stat")).items():
        stat[k] = stat.get(k, 0) + v - start_stat.get(k, 0)
    if stat:
        put("stat", ",".join(f"{k}={v}" for k, v in stat.items()))

    ach = [x for x in body(lines, "ach").split(",") if x]
    ach += [x for x in body(end_lines, "ach").split(",") if x and x not in ach]
    if ach:
        put("ach", ",".join(ach))
    achat = _pairs(body(lines, "achat"))
    for k, v in _pairs(body(end_lines, "achat")).items():
        achat[k] = min(v, achat.get(k, v))  # keep the earliest unlock
    if achat:
        put("achat", ",".join(f"{k}={v}" for k, v in sorted(achat.items())))
    if zones:
        return share_zones(share_talent_xp("\n".join(merge_zones(lines, end_lines))))
    return share_talent_xp("\n".join(lines))


def share_progress(key):
    """Applies account-wide progression to the save before the game starts."""
    if not SAVE.exists():
        return
    text = load(key)
    new = share_zones(share_talent_xp(text))
    if new != text:
        backup = SAVE.with_name("characters.before-share.txt")
        if not backup.exists():
            backup.write_bytes(SAVE.read_bytes())
        dump(key, new)


if __name__ == "__main__":
    k = read_key()
    if sys.argv[1:] == ["dump"]:
        sys.stdout.reconfigure(encoding="utf-8")
        print(load(k), end="")
    elif sys.argv[1] == "load":
        dump(k, Path(sys.argv[2]).read_text(encoding="utf-8"))
