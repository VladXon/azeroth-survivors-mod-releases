"""Keeps the Codex bans of the mod's own jewels across game restarts.

The exe holds the ban mask in memory (patches/custom_jewels.py: block "AZSBANS1", u32 mask
at +16, bit i = the i-th id of bans_ids in AzerothSurvivors-52.feed.json) and the Codex
toggles it. sync() pushes the saved bans into each game it finds and saves every change,
by item id, to %APPDATA%\\wenilla-rogue\\mod_bans.json, so reordering or removing mod
jewels can't shift a ban onto another item.
"""
import ctypes
import json
import os
import struct
import threading
import time
from pathlib import Path

import pvp

SAVED = Path(os.environ["APPDATA"]) / "wenilla-rogue" / "mod_bans.json"
MAGIC = b"AZSBANS1"
VERSION = 1
MASK = 16
ACCESS = pvp.PROCESS_VM_READ | pvp.PROCESS_VM_WRITE | pvp.PROCESS_VM_OPERATION | pvp.PROCESS_QUERY_LIMITED


def to_mask(ids, banned):
    return sum(1 << i for i, x in enumerate(ids) if x in banned)


def load(path=SAVED):
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("banned", []))
    except (OSError, ValueError, AttributeError):
        return set()


def store(ids, mask, path=SAVED):
    """Saves the mask as ids; bans of ids this build doesn't have are kept."""
    banned = (load(path) - set(ids)) | {x for i, x in enumerate(ids) if mask >> i & 1}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"banned": sorted(banned)}), encoding="utf-8")
    tmp.replace(path)


def _rw(handle, va, data_or_size):
    if isinstance(data_or_size, int):
        buf = ctypes.create_string_buffer(data_or_size)
        got = ctypes.c_size_t()
        ok = pvp.k32.ReadProcessMemory(handle, ctypes.c_void_p(va), buf, data_or_size, ctypes.byref(got))
        return buf.raw if ok and got.value == data_or_size else None
    done = ctypes.c_size_t()
    ok = pvp.k32.WriteProcessMemory(handle, ctypes.c_void_p(va), data_or_size, len(data_or_size), ctypes.byref(done))
    return bool(ok) and done.value == len(data_or_size)


def sync(stop=None, until_exit=True, poll=0.5, find_every=2.0, give_up=120.0):
    """Runs until stop is set; with until_exit, also once the game it attached to exits
    (or none showed up within give_up seconds)."""
    stop = stop or threading.Event()
    try:
        info = json.loads(pvp.FEED_INFO.read_text())
        rva, ids = info["bans_rva"], info["bans_ids"]
    except (OSError, ValueError, KeyError):
        return  # an exe without mod jewels: nothing to keep
    head = MAGIC + struct.pack("<II", VERSION, len(ids))
    handle, pid, last, game, seen = None, None, None, None, None
    started = found_at = time.monotonic()
    while not stop.is_set():
        if time.monotonic() - found_at >= find_every or not game:
            game, found_at = pvp.find_game(), time.monotonic()
        if game and game[0] != pid:
            if handle:
                pvp.k32.CloseHandle(handle)
            handle = pvp.k32.OpenProcess(ACCESS, False, game[0]) or None
            va = game[1] + rva
            # ours and the same layout as feed.json: push the saved bans; retry until that works
            if handle and _rw(handle, va, 16) == head:
                mask = to_mask(ids, load())
                if _rw(handle, va + MASK, struct.pack("<I", mask)):
                    pid, last, seen = game[0], mask, True
        elif game and handle:
            raw = _rw(handle, va + MASK, 4)
            mask = struct.unpack("<I", raw)[0] if raw else None
            if mask is None:
                game = None  # gone or unreadable: look it up again
            elif mask != last:
                store(ids, mask)
                last = mask
        if not game:
            if handle:
                pvp.k32.CloseHandle(handle)
            handle, pid = None, None
            if until_exit and (seen or time.monotonic() - started > give_up):
                return
        stop.wait(poll)
    if handle:
        pvp.k32.CloseHandle(handle)


if __name__ == "__main__":
    ids = ["difficulty", "thorns", "x"]
    assert to_mask(ids, {"thorns", "gone"}) == 2
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        f = Path(t) / "b.json"
        f.write_text(json.dumps({"banned": ["gone", "difficulty"]}))
        store(ids, 0b110, f)  # difficulty unbanned, thorns + x banned, "gone" kept
        assert load(f) == {"gone", "thorns", "x"}, load(f)
        assert to_mask(ids, load(f)) == 0b110
    print("bans self-check ok")
