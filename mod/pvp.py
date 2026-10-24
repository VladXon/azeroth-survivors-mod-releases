"""PvP kill race and co-op companion: a small always-on-top window next to the game.

Reads the run state the game streams to its addons (AZSRUN/AZSEND/AZSZONE lines),
which patches/pvp_feed.py copies into a buffer inside AzerothSurvivors-52.exe, and
syncs it with the match server (server/). PvP: more kills wins when both runs end.
Co-op: both play the host's zone together; the partner's hero position is relayed
over a WebSocket and written into the exe's ghost buffer (patches/coop_ghost.py).
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import logging
import logging.handlers
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path

import save
from choice_shield import choice_owner

HERE = Path(__file__).resolve().parent
GAME_EXE = os.environ.get("AZS_GAME_EXE") or "AzerothSurvivors-52.exe"  # override: test copies of the exe
GAME_EXES = {"azerothsurvivors.exe", GAME_EXE.lower()}
DEFAULT_PRESET = {"density": 125, "health": 125, "damage": 100, "speed": 100}
GHOST_HZ = 20
GHOST_FRAME_GAP_S = 1 / GHOST_HZ
GHOST_CONNECT_GRACE_S = 15  # allow the partner to launch/join before probing a quiet socket
FRAME_MAX = 8000  # bytes; the relay (server/) refuses frames over 8192 (relay 3; 4096 before)
GHOST_STALE_S = 3  # the ghost hides when the partner sent nothing for this long
PLAYOUT_S = 0.2  # the partner is shown this far behind its newest frame: frames late by up to this still move it smoothly
OFFSET_CREEP = 0.002  # s per partner sample (~20 ms a second): how fast the jitter buffer follows a slower network
DEAD_SHOW_S = 4  # a dead partner stays visible (flag dead) this long, then the ghost hides
IN_RUN = ("playing", "choosing", "paused")
RUN_OVER = ("died", "finished", "timeout", "forfeit", "left")  # server statuses after a player's run
# co-op run clock (the guest's follows the host's): gaps under TOLERANCE are left alone, gaps over JUMP
# either way (a late start, a stalled host) are jumped, any other is slewed in steps of at most STEP every
# EVERY seconds: +-30 % clock speed, small enough that the enemies, which move on this clock, don't jerk
CLOCK_TOLERANCE_S, CLOCK_JUMP_S, CLOCK_STEP_S, CLOCK_EVERY_S = 0.15, 1.0, 0.075, 0.25
CLOCK_JUMP_HOLD_S = 1.0  # after a jump: the exe has surely applied it before the gap is measured again
HEARTBEAT_S = 45  # co-op progress is re-sent this often even unchanged (a paused run clock stands still)
LABEL_WRAP = 420  # px, the companion window's widest text line
FRESH_HOST_T = 60  # s: a host frame `playing` with a run clock under this is the match's run, not an old one
RACES = {"human": 1, "orc": 2, "dwarf": 3, "nightelf": 4, "undead": 5, "scourge": 5, "tauren": 6, "gnome": 7, "troll": 8}
CLASS_IDS = {"warrior": 1, "paladin": 2, "hunter": 3, "rogue": 4, "priest": 5, "shaman": 7, "mage": 8,
             "warlock": 9, "druid": 11}
PRESET_ORDER = ["density", "health", "damage", "speed"]  # the order of the save's Run Options
FEED_INFO = HERE.parent / "AzerothSurvivors-52.feed.json"
CONFIG = Path(os.environ["APPDATA"]) / "wenilla-rogue" / "pvp.json"
LOG_FILE = CONFIG.parent / "pvp-log.txt"  # what the companion saw and did, for bug reports
log = logging.getLogger("pvp")
SERVER_FILE = HERE / "pvp_server.txt"  # the deployed worker's URL
SERVER = os.environ.get("AZS_PVP_SERVER") or (SERVER_FILE.read_text().strip() if SERVER_FILE.exists() else "")
# Cloudflare blocks the default Python-urllib user agent (error 1010)
USER_AGENT = "AzerothSurvivorsMod/1.0"
POLL_S = 2.5
AUTH_ERROR = "Сервер не принял прежний сетевой токен. Зарегистрируй ник заново; сохранения игры сохраняются."

ZONE_NAMES = {
    "elwynn": "Элвиннский лес", "duskwood": "Сумеречный лес", "wsg": "Ущелье Песни Войны",
    "ungoro": "Кратер Ун'Горо", "ab": "Низина Арати", "feralas": "Фералас", "arena": "Арена Гурубаши",
    "sm": "Монастырь Алого ордена", "av": "Альтеракская долина", "brm": "Черная гора",
    "aq": "Врата Ан'Киража", "naxx": "Наксрамас",
}
STATUS_NAMES = {
    "waiting": "ещё не начал", "running": "играет", "paused": "пауза", "died": "погиб",
    "finished": "прошёл зону", "timeout": "время вышло", "forfeit": "не стартовал", "left": "вышел",
}
PHASE_NAMES = {"playing": "в бою", "choosing": "выбирает карту", "paused": "пауза", "dead": "погиб",
               "victory": "победа", "ready": "готовится"}


# ---------- game feed (ReadProcessMemory) ----------

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
TH32CS_SNAPPROCESS, TH32CS_SNAPMODULE = 0x2, 0x8
PROCESS_VM_READ, PROCESS_QUERY_LIMITED = 0x10, 0x1000
PROCESS_VM_OPERATION, PROCESS_VM_WRITE = 0x8, 0x20
INVALID_HANDLE = ctypes.c_void_p(-1).value
GAME_TITLE = "Azeroth Survivors"


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260)]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD), ("th32ProcessID", wt.DWORD),
                ("GlblcntUsage", wt.DWORD), ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wt.DWORD), ("hModule", wt.HMODULE), ("szModule", wt.WCHAR * 256),
                ("szExePath", wt.WCHAR * 260)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    # the union's biggest member (MOUSEINPUT) is 8 bytes longer than KEYBDINPUT
    _fields_ = [("type", wt.DWORD), ("ki", KEYBDINPUT), ("pad", ctypes.c_ubyte * 8)]


k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
k32.Module32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.ReadProcessMemory.argtypes = [wt.HANDLE, wt.LPCVOID, wt.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.WriteProcessMemory.argtypes = [wt.HANDLE, wt.LPVOID, wt.LPCVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def snapshot(flags, pid):
    snap = k32.CreateToolhelp32Snapshot(flags, pid)
    return None if snap in (None, INVALID_HANDLE) else snap


def processes():
    """{lowercase exe name: pid} of the running processes."""
    snap = snapshot(TH32CS_SNAPPROCESS, 0)
    if snap is None:
        return {}
    entry = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
    out = {}
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        out[entry.szExeFile.lower()] = entry.th32ProcessID
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return out


def game_running():
    return bool(GAME_EXES & set(processes()))


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]


def close_game(timeout=10):
    """Asks the running game to close (WM_CLOSE, so it exits on its own terms) and kills it
    if it is still there after `timeout` s. True once no game process is left."""
    pids = {pid for name, pid in processes().items() if name in GAME_EXES}

    def ask(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        return True
    user32.EnumWindows(WNDENUMPROC(ask), 0)
    deadline = time.monotonic() + timeout
    while game_running() and time.monotonic() < deadline:
        time.sleep(0.5)
    for name, pid in processes().items():
        if name in GAME_EXES:
            handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if handle:
                k32.TerminateProcess(handle, 1)
                k32.CloseHandle(handle)
    deadline = time.monotonic() + 5
    while game_running() and time.monotonic() < deadline:
        time.sleep(0.2)
    return not game_running()


def find_game():
    """(pid, module base) of the running modded game, or None."""
    pid = processes().get(GAME_EXE.lower())
    if pid is None:
        return None
    snap = snapshot(TH32CS_SNAPMODULE, pid)
    if snap is None:
        return None
    mod = MODULEENTRY32W(dwSize=ctypes.sizeof(MODULEENTRY32W))
    base = mod.modBaseAddr if k32.Module32FirstW(snap, ctypes.byref(mod)) else None
    k32.CloseHandle(snap)
    return (pid, base) if base else None


class Feed:
    """Latest AZSRUN / AZSEND / AZSZONE lines from the game, parsed into dicts."""
    SLOTS = ["AZSRUN", "AZSEND", "AZSZONE"]
    SLOT_SIZE = 1024

    def __init__(self):
        info = json.loads(FEED_INFO.read_text())
        self.rva = info["rva"]
        self.handle = None
        self.pid = None
        self.base = None

    def read(self):
        """{prefix: (seq, fields)} or None when the game isn't running."""
        if self.handle is None:
            game = find_game()
            if not game:
                return None
            self.pid, self.base = game
            self.handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_LIMITED, False, self.pid)
            if not self.handle:
                self.handle = None
                return None
        size = 8 + self.SLOT_SIZE * len(self.SLOTS)
        for _ in range(5):
            raw = self._read(size)
            again = self._read(size)
            if raw is None or again is None:  # the game exited
                k32.CloseHandle(self.handle)
                self.handle = None
                return None
            if raw[:8] != b"AZSFEED1":
                return None
            out = {}
            stable = True
            for i, prefix in enumerate(self.SLOTS):
                seq, length = struct.unpack_from("<II", raw, 8 + i * self.SLOT_SIZE)
                seq2 = struct.unpack_from("<I", again, 8 + i * self.SLOT_SIZE)[0]
                if seq % 2 or seq != seq2:
                    stable = False
                    break
                body = raw[16 + i * self.SLOT_SIZE:16 + i * self.SLOT_SIZE + min(length, 1016)]
                out[prefix] = (seq, parse_fields(body.decode("utf-8", "replace")))
            if stable:
                return out
        return None

    def _read(self, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(self.handle, ctypes.c_void_p(self.base + self.rva), buf, size, ctypes.byref(got))
        return buf.raw if ok and got.value == size else None


def parse_fields(body):
    """'k=v;k=v' as the game's AZS_Fields does."""
    out = {}
    for part in body.split(";"):
        k, sep, v = part.partition("=")
        if sep:
            out[k] = int(v) if v.lstrip("-").isdigit() else v
    return out


class Ghost:
    """The exe's co-op ghost block at module base + ghost_rva (little-endian, seqlock: seq+1 odd,
    fields, seq+2 even, so the exe never reads a torn update; zeros are the exe's defaults):
    +0 magic (exe-owned) "AZSGHST1" (v1) or "AZSGHST2" (v2) | +8 u32 seq | +12 u32 active |
    +16 f32 x, y, z, facing | +32 u32 race, class, gender, display_hint
    v2 only: +48 u8 look[5] (skin, face, hair style, hair colour, facial hair), 3 pad | +56 u32 level |
    +60 f32 dps | +64 u32 flags (1 fighting, 2 dead, 4 paused) | +68 u32 spell_hint | +72 u32 items[19] |
    +148 nick[16] utf-8 | +164 u32 summon_count | +168 4 x (u32 kind, f32 dx, f32 dy) |
    +216 u32 control (bit0 END_RUN_DEFEAT: the partner died, end my run too) | +220 u8 lang of MY game
    (0 English, 1 Russian: the exe's "ally has fallen" text) | +221.. reserved.
    v3 only: flags bit3 = the partner streams real hits (the exe replays them instead of dps) |
    +224 u32 hp, max hp | +232 u32 grants[2] (chest, guardian picks the partner got, a running count:
    the exe gives me one pick per step) | +256 u32 ev_head (mine), +260 u32 ev_tail (the exe's) |
    +264 EV_RING events outside the seqlock (EVENT: u32 spell, f32 amount, x, y, u32 school,
    u32 crit | kind << 8 | type << 16), the partner's hits (type 0) and casts (1 at a unit, 2 at a
    point, 3 no target) | +240 f32 clock adjust, u32 adjust seq (run sync: the exe adds the adjust
    to its run clock once per new seq), +248 f32 the partner's run clock at its death (0: unknown; the
    shared defeat ends my run on it) | exes with ghost_sync in the feed json also publish +3336 f32
    run clock, u32 phase (0 ready .. 5 victory) after every run tick and take control bits 1 COOP_LIVE
    (no pause, clock adjust), 2 HOLD_START (my first step doesn't start the run), 3 START_NOW.
    Exes with boss_sync: +3344 u32 on (the partner streams boss health), u32 count, +3352 BOSS_SLOTS x
    (u32 key, f32 health fraction): the partner's bosses, outside the seqlock (each entry one u64)."""
    VERSIONS = {b"AZSGHST1": 1, b"AZSGHST2": 2, b"AZSGHST3": 3}
    V1 = struct.Struct("<I4f4I")  # +12: active, x, y, z, facing, race, class, gender, display_hint
    V2 = struct.Struct("<5B3xIfII19I16sI")  # +48: look, level, dps, flags, spell_hint, items, nick, summon_count
    SUMMON = struct.Struct("<Iff")  # +168, 4 of them
    V3 = struct.Struct("<3xII2I")  # +221: pad, hp, max hp, grants
    EVENT = struct.Struct("<IfffII")
    CLOCK = struct.Struct("<fIf")  # +240
    EV_HEAD, EV_RING, EV_AT = 256, 128, 264
    RUN_AT = 3336
    BOSS_AT, BOSS_SLOTS = 3344, 8
    BOSS = struct.Struct("<If")
    END_RUN_DEFEAT, COOP_LIVE, HOLD_START, START_NOW = 1, 2, 4, 8
    FLAG_EVENTS = 8
    CHOICE_SIZE = 160
    CHOICE_MAGIC = b"AZSCHOI1"

    def __init__(self, rva, sync=False, boss=False, choice_rva=None):
        self.rva = rva
        self.sync = sync  # the exe has the run sync (no pause, start gate, clock)
        self.boss = boss  # the exe has the boss sync (shared boss health, bosses chase the nearer hero)
        self.choice_rva = choice_rva if isinstance(choice_rva, int) and choice_rva > 0 else None
        self.handle = None
        self.base = None
        self.seq = None
        self.version = None

    def write(self, ally, control=0, lang=0, grants=(0, 0), clock=(0.0, 0, 0.0), bosses=None):
        """ally = the partner (keys below, missing ones are defaults) to show, or None to hide;
        control = the v2 control word, grants = the v3 pick counts, clock = (adjust, adjust seq, the
        partner's final run clock), bosses = the partner's [key, fraction] pairs (None: it doesn't stream
        them). False when the game isn't running (or isn't a ghost build)."""
        if self.handle is None and not self._open():
            return False
        fields = self.pack(ally, self.version, control, lang, grants, clock)
        ok = (self._write(8, struct.pack("<I", (self.seq + 1) & 0xFFFFFFFF))
              and self._write(12, fields)
              and self._write(8, struct.pack("<I", (self.seq + 2) & 0xFFFFFFFF))
              and (not self.boss or self._write(self.BOSS_AT, self.pack_bosses(bosses if ally else None))))
        if not ok:
            self.close()
            return False
        self.seq = (self.seq + 2) & 0xFFFFFFFF
        return True

    @classmethod
    def pack(cls, ally, version, control=0, lang=0, grants=(0, 0), clock=(0.0, 0, 0.0)):
        """Bytes from +12 on for that block version."""
        a = ally or {}
        f = lambda k, lim: clamp(a.get(k, 0), lim)
        u = lambda v, hi=0xFFFFFFFF: int(v) if isinstance(v, (int, float)) and 0 <= v <= hi and float(v).is_integer() else 0
        out = cls.V1.pack(1 if ally else 0, f("x", 1e5), f("y", 1e5), f("z", 1e5), f("facing", 10),
                          u(a.get("race", 0)), u(a.get("class", 0)), u(a.get("gender", 0)), 0)
        if version == 1:
            return out
        look = (list(a.get("look") or []) + [0] * 5)[:5]
        items = (list(a.get("items") or []) + [0] * 19)[:19]
        summons = [s for s in (a.get("summons") or []) if u(s.get("kind", 0), 9)][:4]
        out += cls.V2.pack(*(u(v, 255) for v in look), u(a.get("lvl", 0)), max(0.0, f("dps", 1e6)),
                           u(a.get("flags", 0), 15 if version >= 3 else 7), 0, *(u(v, 2 ** 24 - 1) for v in items),
                           nick_bytes(a.get("nick", "")), len(summons))
        for i in range(4):
            s = summons[i] if i < len(summons) else {}
            out += cls.SUMMON.pack(u(s.get("kind", 0), 9), clamp(s.get("dx", 0), 100), clamp(s.get("dy", 0), 100))
        out += struct.pack("<IB", control, 1 if lang == 1 else 0)
        if version == 2:
            return out
        out += cls.V3.pack(u(a.get("hp", 0)), u(a.get("hpm", 0)), *(u(g) for g in grants))
        return out + cls.CLOCK.pack(clamp(clock[0], 1e5), clock[1] & 0xFFFFFFFF, max(0.0, clamp(clock[2], 1e7)))

    @classmethod
    def pack_bosses(cls, bosses):
        """+3344 on, count, then the entries (zeros after them)."""
        bosses = boss_health(bosses)
        on = bosses is not None
        bosses = bosses or []
        return (struct.pack("<II", int(on), len(bosses)) + b"".join(cls.BOSS.pack(k, f) for k, f in bosses)
                + bytes(cls.BOSS.size * (cls.BOSS_SLOTS - len(bosses))))

    def run_state(self):
        """(run clock, phase number) the exe published after its last run tick; None without run sync."""
        if not self.sync or (self.handle is None and not self._open()):
            return None
        raw = self._read(self.RUN_AT + 8)
        return struct.unpack_from("<fI", raw, self.RUN_AT) if raw else None

    def choice_state(self):
        """Read a stable exe-published choice snapshot; None when unsupported or mid-write."""
        if self.choice_rva is None or (self.handle is None and not self._open()):
            return None
        raw = None
        for _ in range(3):
            sample = self._read_at(self.choice_rva, 148)
            if not sample or len(sample) != 148 or sample[:8] != self.CHOICE_MAGIC:
                continue
            output_seq, = struct.unpack_from("<I", sample, 144)
            if output_seq & 1:
                continue
            check = self._read_at(self.choice_rva + 144, 4)
            if check is None or len(check) != 4 or struct.unpack("<I", check)[0] != output_seq:
                continue
            raw = sample[:48]
            break
        if raw is None:
            return None
        enabled, peer_flags, serial, phase, remaining, real_t, seq, flags, peer_serial, peer_remaining = \
            struct.unpack_from("<IIIIffIIIf", raw, 8)
        if not all(math.isfinite(v) for v in (remaining, real_t, peer_remaining)):
            return None
        return {"enabled": enabled, "peer_flags": peer_flags, "serial": serial, "phase": phase,
                "remaining": max(0.0, min(4.0, remaining)), "real_t": real_t, "seq": seq, "flags": flags,
                "peer_serial": peer_serial, "peer_remaining": max(0.0, min(4.0, peer_remaining))}

    def write_choice(self, enabled, peer_flags, serial, remaining):
        """Publish companion-owned choice inputs, then advance the block's input sequence."""
        state = self.choice_state()
        if state is None:
            return False
        seq = (state["seq"] + 1) & 0xFFFFFFFF
        ok = (self._write_at(self.choice_rva + 8, struct.pack("<I", int(bool(enabled))))
              and self._write_at(self.choice_rva + 12, struct.pack("<I", int(peer_flags) & 7))
              and self._write_at(self.choice_rva + 40, struct.pack("<I", int(serial) & 0xFFFFFFFF))
              and self._write_at(self.choice_rva + 44, struct.pack("<f", max(0.0, min(4.0, float(remaining)))))
              and self._write_at(self.choice_rva + 32, struct.pack("<I", seq)))
        return bool(ok)

    def push_events(self, events):
        """Appends the partner's events [spell, amount, crit, school, x, y, kind, type] to the v3
        ring, as many as the exe has room for (the rest are dropped). Number written; 0 without v3."""
        if self.version != 3 or not events:
            return 0
        raw = self._read(self.EV_HEAD + 8)
        if raw is None:
            return 0
        head, tail = struct.unpack_from("<II", raw, self.EV_HEAD)
        room = max(0, self.EV_RING - ((head - tail) & 0xFFFFFFFF))
        events = events[:room]
        for i, e in enumerate(events):
            spell, amount, crit, school, x, y, kind, etype = e
            slot = self.EV_AT + self.EVENT.size * ((head + i) % self.EV_RING)
            info = (crit & 1) | (kind & 0xFF) << 8 | (etype & 0xFF) << 16
            if not self._write(slot, self.EVENT.pack(spell, amount, x, y, school, info)):
                return 0
        # entries first, then the head that publishes them
        if events and not self._write(self.EV_HEAD, struct.pack("<I", (head + len(events)) & 0xFFFFFFFF)):
            return 0
        return len(events)

    def _open(self):
        game = find_game()
        if not game:
            return False
        pid, self.base = game
        access = PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION | PROCESS_QUERY_LIMITED
        self.handle = k32.OpenProcess(access, False, pid) or None
        head = self._read(12) if self.handle else None
        self.version = self.VERSIONS.get(head[:8]) if head else None
        if not self.version:  # not a ghost build: never write blind
            self.close()
            return False
        self.seq = struct.unpack_from("<I", head, 8)[0] & ~1
        return True

    def close(self):
        if self.handle:
            k32.CloseHandle(self.handle)
        self.handle = None

    def _read(self, size):
        return self._read_at(self.rva, size)

    def _read_at(self, rva, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(self.handle, ctypes.c_void_p(self.base + rva), buf, size, ctypes.byref(got))
        return buf.raw if ok and got.value == size else None

    def _write(self, offset, data):
        return self._write_at(self.rva + offset, data)

    def _write_at(self, rva, data):
        done = ctypes.c_size_t()
        ok = k32.WriteProcessMemory(self.handle, ctypes.c_void_p(self.base + rva), data, len(data),
                                    ctypes.byref(done))
        return bool(ok) and done.value == len(data)


class Hits:
    """The hits and casts of my hero and the reward picks it gets, from the exe's "AZSHITS1" block
    (patches/coop_ghost.py, rva hits_rva in the feed json), read through the Feed's process handle:
    +8 u32 head (events written so far) | +12 u32 picks[2] (chests bought, guardians slain so far,
    never the picks the partner gave) | +24 RING x Ghost.EVENT | exes with boss_sync: then
    Ghost.BOSS_SLOTS x Ghost.BOSS, my bosses (key 0: an empty slot, fraction 0: dead or gone)."""
    RING, AT = 256, 24
    EVENT = Ghost.EVENT
    BOSS_AT = AT + RING * EVENT.size

    def __init__(self, feed, rva, boss=False):
        self.feed, self.rva = feed, rva
        self.boss = boss
        self.pid = None
        self.last = 0

    def read(self):
        """(new events as [spell, amount, crit, school, x, y, kind, type], picks, my bosses as (key,
        fraction) or None without the boss sync) or None."""
        if self.feed.handle is None:
            return None
        raw = self._read(self.BOSS_AT + (Ghost.BOSS_SLOTS * Ghost.BOSS.size if self.boss else 0))
        if raw is None or raw[:8] != b"AZSHITS1":
            return None
        head = struct.unpack_from("<I", raw, 8)[0]
        picks = list(struct.unpack_from("<2I", raw, 12))
        if self.pid != self.feed.pid:  # a new game process: only what comes from now on
            self.pid, self.last = self.feed.pid, head
        new = (head - self.last) & 0xFFFFFFFF
        start = (head - min(new, self.RING)) & 0xFFFFFFFF  # lapped: the latest RING only
        self.last = head
        out = []
        for i in range(min(new, self.RING)):
            spell, amount, x, y, school, info = self.EVENT.unpack_from(
                raw, self.AT + self.EVENT.size * ((start + i) % self.RING))
            if math.isfinite(amount) and math.isfinite(x) and math.isfinite(y):
                out.append([spell, round(amount, 1), info & 1, school, round(x, 1), round(y, 1),
                            info >> 8 & 0xFF, info >> 16 & 0xFF])
        bosses = None
        if self.boss:
            # a live boss never rounds to 0: that means dead
            bosses = [[k, max(round(f, 5), 1e-5) if f > 0 else 0.0]
                      for k, f in (Ghost.BOSS.unpack_from(raw, self.BOSS_AT + Ghost.BOSS.size * i)
                                   for i in range(Ghost.BOSS_SLOTS)) if k]
        return out, picks, boss_health(bosses)

    def _read(self, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(self.feed.handle, ctypes.c_void_p(self.feed.base + self.rva), buf, size,
                                   ctypes.byref(got))
        return buf.raw if ok and got.value == size else None


class SharedBlock:
    """The exe's shared enemies block "AZSENMY1" (patches/coop_shared.py, rva shared_rva), through the
    Ghost's process handle: my shared enemies (exe-written, seqlock) and the partner's side (mine to write).
    Entries (id, template, flags, x, y, z, end of the move on the run clock, health fraction)."""
    ENTRY = struct.Struct("<IBBxxfffff")
    MAX, MINE = 200, 32
    IN = MINE + MAX * ENTRY.size

    def __init__(self, ghost, rva):
        self.ghost, self.rva = ghost, rva
        self.seq = 0

    def read(self):
        """(run clock, my hero (x, y, z), entries) the exe published last, or None."""
        if not self.ghost.handle:
            return None
        for _ in range(4):
            raw = self._read(0, self.IN)
            if raw is None or raw[:8] != b"AZSENMY1":
                return None
            seq, count, clock, hx, hy, hz = struct.unpack_from("<IIffff", raw, 8)
            if seq % 2 == 0 and self._read(8, 4) == raw[8:12]:
                entries = [self.ENTRY.unpack_from(raw, self.MINE + i * self.ENTRY.size) for i in range(min(count, self.MAX))]
                return clock, (hx, hy, hz), entries
        return None

    def write(self, entries, complete):
        """The partner's side for the exe's tick (seqlock: odd while writing)."""
        if not self.ghost.handle:
            return False
        entries = entries[:self.MAX]
        body = struct.pack("<III", len(entries), 1 if complete and len(entries) < self.MAX else 0, 0) \
            + b"".join(self.ENTRY.pack(*e) for e in entries)
        ok = (self._write(self.IN, struct.pack("<I", (self.seq + 1) & 0xFFFFFFFF)) and self._write(self.IN + 4, body)
              and self._write(self.IN, struct.pack("<I", (self.seq + 2) & 0xFFFFFFFF)))
        self.seq = (self.seq + 2) & 0xFFFFFFFF
        return ok

    def _read(self, offset, size):
        buf = ctypes.create_string_buffer(size)
        got = ctypes.c_size_t()
        ok = k32.ReadProcessMemory(self.ghost.handle, ctypes.c_void_p(self.ghost.base + self.rva + offset), buf, size,
                                   ctypes.byref(got))
        return buf.raw if ok and got.value == size else None

    def _write(self, offset, data):
        done = ctypes.c_size_t()
        ok = k32.WriteProcessMemory(self.ghost.handle, ctypes.c_void_p(self.ghost.base + self.rva + offset), data,
                                    len(data), ctypes.byref(done))
        return bool(ok) and done.value == len(data)


# shared enemies (relay 3): the host's table `en` and the guest's lower health fractions `ef`, base64
SH_DEAD, SH_LURE, SH_NEW_SEG, SH_GONE = 1, 2, 4, 8  # the exe's partner-table flags (coop_shared.py)
SH_LIVE, SH_GUEST, SH_SOLO = 16, 32, 64  # control bits
SH_MIRROR = 0x800000  # the guest's mirrors: host id | this
SH_BOSS = 0x1000000  # bosses and guardians in the exe's tables: this | boss-sync key
EN_DEAD, EN_GUEST_OWNS, EN_BOSS = 1, 2, 4  # flags on the wire
# en: sig, clock, base x, y, z | per enemy: id (24 bits), template, dx, dy, dz (1/4 yd from the base),
# fraction (1/65535, 0 dead), flags, rest of its move (10 ms)
EN_HEAD, EN_ENTRY = struct.Struct("<Iffff"), struct.Struct("<HBBhhhHBB")
EF_HEAD, EF_ENTRY = struct.Struct("<I"), struct.Struct("<HBH")  # ef: sig | id, fraction (EF_GONE: vanished)
EF_GONE = 0xFFFF
CHOICE_EF_IDS = (0xFFFFFE, 0xFFFFFD, 0xFFFFFC)
CHOICE_EF_MAGIC = 0xC501
SH_HOLD, EN_HOLD = 16, 8  # hold the existing owner/mirror in place during a protected pick
SHARED_EPS = 0.002  # fractions closer than this are equal (the exe's EPS)
SHARED_STALE_S, SHARED_SOLO_S = 3.0, 10.0  # guest: the host's table this old -> own AI on them, then own spawner
OWNER_SHARE, OWNER_YD = 0.85, 5.0  # an enemy changes hands when the other hero is this much nearer
TOMB_S = 300.0  # guest: how long a mirror I killed (or lost) stays dead for the host's table
NEW_SEG_YD, NEW_SEG_S = 0.5, 0.1  # guest: a host move this different from the last one is a new move


def pack_en(sig, clock, base, entries):
    """The host's table for the wire: entries (id, template, flags, x, y, z, end, fraction) relative to base."""
    q = lambda v, b: max(-32768, min(32767, round((v - b) * 4)))
    out = EN_HEAD.pack(sig, clock, *base)
    for eid, tpl, flags, x, y, z, end, frac in entries:
        if not all(map(math.isfinite, (x, y, z, end, frac))):
            continue  # a record the game hasn't filled yet
        dead = flags & EN_DEAD or not frac > 0
        flags = (flags & (EN_DEAD | EN_GUEST_OWNS | EN_HOLD)) | (EN_BOSS if eid & SH_BOSS else 0)
        out += EN_ENTRY.pack(eid & 0xFFFF, eid >> 16 & 0x7F, tpl, q(x, base[0]), q(y, base[1]), q(z, base[2]),
                             0 if dead else max(1, min(65535, math.ceil(frac * 65535))),
                             flags | (EN_DEAD if dead else 0), max(0, min(255, round((end - clock) * 100))))
    return base64.b64encode(out).decode()


def unpack_en(text, sig):
    """(clock, {id: (template, flags, x, y, z, end, fraction)}) of a host's table (bosses: SH_BOSS | key),
    None when it isn't one (another game build's templates, junk)."""
    try:
        raw = base64.b64decode(text, validate=True)
        hsig, clock, bx, by, bz = EN_HEAD.unpack_from(raw)
    except (TypeError, ValueError, struct.error):
        return None
    if hsig != sig or (len(raw) - EN_HEAD.size) % EN_ENTRY.size or not all(map(math.isfinite, (clock, bx, by, bz))):
        return None
    table = {}
    for lo, hi, tpl, dx, dy, dz, frac, flags, rest in EN_ENTRY.iter_unpack(raw[EN_HEAD.size:]):
        if tpl >= 64:  # no zone has that many templates: junk
            continue
        table[hi << 16 | lo | (SH_BOSS if flags & EN_BOSS else 0)] = \
            (tpl, flags, bx + dx / 4, by + dy / 4, bz + dz / 4, clock + rest / 100, frac / 65535)
    return clock, table


def pack_ef(sig, reports):
    """The guest's reports for the wire: (host id, fraction, or None: it vanished)."""
    return base64.b64encode(EF_HEAD.pack(sig) + b"".join(
        EF_ENTRY.pack(i & 0xFFFF, i >> 16 & 0x7F, EF_GONE if f is None else 0 if not f > 0 else max(1, min(65534, math.floor(f * 65535))))
        for i, f in reports)).decode()


def unpack_ef(text, sig):
    """{host id: fraction, or None: vanished} of a guest's reports, None when it isn't one."""
    try:
        raw = base64.b64decode(text, validate=True)
        hsig, = EF_HEAD.unpack_from(raw)
    except (TypeError, ValueError, struct.error):
        return None
    if hsig != sig or (len(raw) - EF_HEAD.size) % EF_ENTRY.size:
        return None
    return {hi << 16 | lo: None if f == EF_GONE else f / 65535 for lo, hi, f in EF_ENTRY.iter_unpack(raw[EF_HEAD.size:])}


def pack_choice_ef(serial, remaining_ms, base=None):
    """Append three reserved choice records to the existing ef field without changing its header."""
    try:
        raw = (base64.b64decode(base, validate=True) if isinstance(base, str) else
               bytes(base) if isinstance(base, (bytes, bytearray)) else EF_HEAD.pack(0))
        if len(raw) < EF_HEAD.size or (len(raw) - EF_HEAD.size) % EF_ENTRY.size:
            raw = EF_HEAD.pack(0)
    except (ValueError, TypeError):
        raw = EF_HEAD.pack(0)
    entries = (EF_ENTRY.pack(CHOICE_EF_IDS[0] & 0xFFFF, 0xFF, CHOICE_EF_MAGIC)
               + EF_ENTRY.pack(CHOICE_EF_IDS[1] & 0xFFFF, 0xFF, int(serial) & 0xFFFF)
               + EF_ENTRY.pack(CHOICE_EF_IDS[2] & 0xFFFF, 0xFF, max(0, min(4000, int(remaining_ms)))))
    return base64.b64encode(raw + entries).decode()


def unpack_choice_ef(text):
    """Return complete capability metadata and an ef value with reserved records removed."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, TypeError):
        return None, text
    if len(raw) < EF_HEAD.size or (len(raw) - EF_HEAD.size) % EF_ENTRY.size:
        return None, text
    rows = list(EF_ENTRY.iter_unpack(raw[EF_HEAD.size:]))
    found = {hi << 16 | lo: value for lo, hi, value in rows if hi == 0xFF}
    if found.get(CHOICE_EF_IDS[0]) != CHOICE_EF_MAGIC or not all(i in found for i in CHOICE_EF_IDS[1:]):
        return None, text
    markers = {"capable": True, "serial": found[CHOICE_EF_IDS[1]],
               "remaining_ms": min(4000, found[CHOICE_EF_IDS[2]])}
    cleaned = bytearray(raw[:EF_HEAD.size])
    for lo, hi, value in rows:
        if hi << 16 | lo not in CHOICE_EF_IDS:
            cleaned += EF_ENTRY.pack(lo, hi, value)
    return markers, base64.b64encode(cleaned).decode()


class ChoicePeerTracker:
    """Monotonic peer-window deadline that duplicate relay frames can only shorten."""
    def __init__(self):
        self.serial = None
        self.deadline = 0.0

    def observe(self, now, markers):
        if not markers or markers.get("capable") is not True:
            return False
        serial = int(markers.get("serial", -1)) & 0xFFFF
        remaining = max(0, min(4000, int(markers.get("remaining_ms", 0)))) / 1000
        if self.serial is None or 0 < ((serial - self.serial) & 0xFFFF) < 0x8000:
            self.serial = serial
            self.deadline = now + remaining
            return True
        if serial == self.serial:
            self.deadline = min(self.deadline, now + remaining)
        return False

    def active(self, now):
        return now < self.deadline


def owner(prev, d_host, d_guest):
    """1 when the guest's game runs the enemy: the nearer hero, handed over only when the other hero is
    nearer by OWNER_SHARE and OWNER_YD (no flapping between two heroes standing close)."""
    if prev is None:
        return int(d_guest < d_host)
    mine, other = (d_guest, d_host) if prev else (d_host, d_guest)
    return 1 - prev if other < mine * OWNER_SHARE and other < mine - OWNER_YD else prev


def guest_bits(age):
    """Guest control bits by the age of the host's last table: live mirrors, then my own AI on them (the
    spawner still closed), then solo with my own spawner."""
    if age < SHARED_STALE_S:
        return SH_LIVE | SH_GUEST
    return SH_GUEST | (SH_SOLO if age >= SHARED_SOLO_S else 0)


class SharedSync:
    """One shared set of enemies for both games (coop_shared.py): the host's table goes to the guest, the
    guest's lower health fractions come back. The host's companion decides who runs each enemy; the guest's
    keeps the ones it killed or lost dead (tombstones) until the host's table agrees. The GhostLink feeds it."""

    def __init__(self, block, sig, host):
        self.block, self.sig, self.host = block, sig, host
        self.on = False  # the partner takes part (host: its frames carry ef; guest: the host's carry en)
        self.table, self.table_at, self.table_clock = {}, 0.0, 0.0  # guest: the host's last table, when it came, its clock
        self.written = {}  # guest: id -> what the exe last got (a change is a new move)
        self.tomb = {}  # guest: host id -> ("dead" | "gone" | "lost", since)
        self.alive = set()  # guest: my mirrors alive at the last read
        self.reports = {}  # host: the guest's last reports
        self.owners = {}  # host: id -> 1 the guest runs it
        self.mine = None  # my exe's last table (clock, hero, entries)
        self.state = None  # the mode last logged
        self.choice = None  # optional host-side shield ownership policy; None preserves legacy routing

    def outgoing(self, now):
        """The fields for my next frame: the host's table or the guest's reports."""
        self.mine = self.block.read()
        if self.mine is None:
            return {}
        clock, hero, entries = self.mine
        if self.host:
            return {"en": pack_en(self.sig, clock, hero, [
                (e[0], e[1], (EN_DEAD if e[2] & SH_DEAD else 0) | (EN_GUEST_OWNS if self.owners.get(e[0]) else 0)
                 | (EN_HOLD if e[2] & SH_HOLD else 0),
                 *e[3:]) for e in entries if not e[0] & SH_MIRROR])}
        return {"ef": pack_ef(self.sig, self.guest_reports(entries, now))}

    def guest_reports(self, entries, now):
        """My mirrors whose health is below the host's (0: I killed it) and the ones of mine that vanished
        (an explosion at my hero); tombstones keep both dead until the host's table agrees."""
        mirrors = {e[0] & (SH_MIRROR - 1): e for e in entries if e[0] & SH_MIRROR}
        reports = []
        for hid, e in mirrors.items():
            host = self.table.get(hid)
            if e[2] & SH_DEAD:
                self.tomb.setdefault(hid, ("dead", now))
            elif host and not host[1] & EN_DEAD and e[7] < host[6] - SHARED_EPS:
                reports.append((hid, e[7]))
        for hid in self.alive - mirrors.keys():
            host = self.table.get(hid)
            if host and not host[1] & EN_DEAD:
                self.tomb.setdefault(hid, ("gone" if host[1] & EN_GUEST_OWNS else "lost", now))
        self.alive = {hid for hid, e in mirrors.items() if not e[2] & SH_DEAD}
        for hid, (kind, since) in list(self.tomb.items()):
            host = self.table.get(hid)
            if not host or host[1] & EN_DEAD or now - since > TOMB_S:
                del self.tomb[hid]
            elif kind != "lost":
                reports.append((hid, 0.0 if kind == "dead" else None))
        return reports

    def incoming(self, message, now):
        """A partner frame: the guest's reports (host) or the host's table (guest)."""
        if self.host and isinstance(message.get("ef"), str):
            reports = unpack_ef(message["ef"], self.sig)
            if reports is not None:
                self.on, self.reports = True, reports
        elif not self.host and isinstance(message.get("en"), str):
            table = unpack_en(message["en"], self.sig)
            if table is not None:
                self.on, self.table, self.table_at, self.table_clock = True, table[1], now, table[0]

    def bits(self, now, partner_live):
        """My control bits: host LIVE while the guest is live; guest by the age of the host's table."""
        if not self.on:
            return 0
        if self.host:
            return SH_LIVE if partner_live else 0
        return guest_bits(now - self.table_at)

    def new_move(self, hid, x, y, z, end, lure):
        """A host move worth sending to my exe: who runs it changed, it heads elsewhere, or a move still under way
        ends at another time (the wire's rounding and a finished move's end, which follows the table's clock,
        are no new move)."""
        old = self.written.get(hid)
        if old is None or old[4] != lure:
            return True
        going = end > self.table_clock + 0.005
        return math.dist(old[:3], (x, y, z)) > NEW_SEG_YD or (going and abs(old[3] - end) > NEW_SEG_S)

    def guest_table(self):
        """The host's table for my exe: who runs each enemy (bosses and guardians too), the dead and vanished
        ones, new moves. The second value is what the exe gets once the write succeeds."""
        out, written = [], {}
        for hid, (tpl, flags, x, y, z, end, frac) in self.table.items():
            lure = not flags & EN_GUEST_OWNS
            # a boss's health and death stay with the boss sync: only its owner and moves come from here
            fl = ((SH_DEAD if flags & EN_DEAD and not hid & SH_BOSS else 0)
                  | (SH_LURE if lure else 0) | (SH_HOLD if flags & EN_HOLD else 0))
            kind = self.tomb.get(hid, (None,))[0]
            if kind:
                fl = SH_DEAD if kind == "dead" else SH_GONE
            if self.new_move(hid, x, y, z, end, lure):
                fl |= SH_NEW_SEG
                written[hid] = (x, y, z, end, lure)
            else:
                written[hid] = self.written[hid]
            out.append((hid, tpl, fl, x, y, z, end, frac))
        return out, written

    def host_table(self, guest_xy):
        """Owners (the nearer hero, with hysteresis; bosses and guardians too) and the guest's reports for my exe."""
        if self.mine is None:
            return []
        _, hero, entries = self.mine
        out, owners = [], {}
        for eid, tpl, flags, x, y, z, end, frac in entries:
            if flags & SH_DEAD or eid & SH_MIRROR:
                continue
            d_host, d_guest = math.dist((x, y), hero[:2]), math.dist((x, y), guest_xy)
            base_owner = owner(self.owners.get(eid), d_host, d_guest)
            choice = self.choice or {}
            preserve_owner = bool(choice.get("both", False)) or bool(
                eid & SH_BOSS and (choice.get("host_shield", False) or choice.get("guest_shield", False)))
            if eid in self.owners and preserve_owner:
                # Keep existing aggro stable during simultaneous picks and keep boss AI from
                # redirecting its established target during either player's shield window.
                base_owner = self.owners[eid]
            target = choice_owner(base_owner, d_host, d_guest, choice.get("host_shield", False),
                                  choice.get("guest_shield", False), choice.get("both", False),
                                  boss=bool(eid & SH_BOSS))
            held = target == 2
            owners[eid] = base_owner if held else target
            fl, frac = (SH_LURE if owners[eid] else 0) | (SH_HOLD if held else 0), 1.0
            if eid in self.reports:
                report = self.reports[eid]
                fl, frac = (fl | SH_GONE, 1.0) if report is None else (fl | (SH_DEAD if report <= 0 else 0), report)
            out.append((eid, tpl, fl, x, y, z, end, frac))
        self.owners = owners
        return out

    def write_exe(self, now, partner, partner_live):
        """The partner's side into my exe, each companion step."""
        if not self.on:
            return
        if self.host:
            state = "host, guest live" if partner_live else "host, guest away"
        else:
            state = {SH_LIVE | SH_GUEST: "guest, mirrors live", SH_GUEST: "guest, host silent: own AI",
                     SH_GUEST | SH_SOLO: "guest, solo"}[guest_bits(now - self.table_at)]
        if state != self.state:
            log.info("shared enemies: %s", state)
            self.state = state
        if not self.host:
            table, written = self.guest_table()
            if self.block.write(table, len(self.table) < SharedBlock.MAX):
                self.written = written
        elif partner_live and partner:
            self.block.write(self.host_table((clamp(partner.get("x"), 1e5), clamp(partner.get("y"), 1e5))), True)


def boss_health(value):
    """The [key, health fraction] pairs of a boss sync frame as (int key, fraction 0..1) tuples, at most
    Ghost.BOSS_SLOTS, bad entries dropped; None when it isn't a list (the sender has no boss sync)."""
    if not isinstance(value, list):
        return None
    out = []
    for e in value[:Ghost.BOSS_SLOTS]:
        if (isinstance(e, (list, tuple)) and len(e) == 2 and isinstance(e[0], int) and 0 < e[0] < 2 ** 24
                and isinstance(e[1], (int, float)) and math.isfinite(e[1])):
            out.append((e[0], min(1.0, max(0.0, float(e[1])))))
    return out


def cap_events(events, cap=64):
    """At most cap events for one relay frame: the biggest hits (casts, amount 0, go first), each
    dropped hit folded into a kept hit of the same spell, so every kept spell's damage stays whole."""
    if len(events) <= cap:
        return events
    ranked = sorted(events, key=lambda e: -e[1])
    kept = [list(e) for e in ranked[:cap]]
    by_spell = {}
    for e in kept:
        if e[7] == 0:
            by_spell.setdefault(e[0], []).append(e)
    for i, e in enumerate(ranked[cap:]):
        same = by_spell.get(e[0]) if e[7] == 0 else None
        if same:
            target = same[i % len(same)]
            target[1] = round(target[1] + e[1], 1)
    return kept


def clamp(v, lim):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(-lim, min(lim, v)) if math.isfinite(v) else 0.0


def game_lang(game, default=0):
    """1 when my game runs in Russian (Cyrillic in AZSZONE zname/cname), 0 for English, else default."""
    zone = (game or {}).get("AZSZONE", (0, {}))[1]
    names = f"{zone.get('zname') or ''}{zone.get('cname') or ''}"
    if not names:
        return default
    return 1 if any(0x400 <= ord(ch) <= 0x4FF for ch in names) else 0  # the Cyrillic block


def nick_bytes(nick):
    """UTF-8, at most 16 bytes, cut on a character boundary (struct pads it with NULs)."""
    return str(nick).encode()[:16].decode("utf-8", "ignore").encode()


def hero_static(text):
    """Our hero's looks for the partner from the save: pick = class|race|gender|zone|name|look5|options,
    equip|<class>|slot:item,... for the picked class. None without a pick line."""
    lines = text.split("\n")
    pick = next((l.split("|") for l in lines if l.startswith("pick|")), None)
    if not pick or len(pick) < 7:
        return None
    num = lambda v: int(v) if str(v).lstrip("-").isdigit() else 0
    items = [0] * 19
    equip = next((l for l in lines if l.startswith(f"equip|{pick[1]}|")), "")
    for entry in equip.split("|", 2)[2].split(",") if equip.count("|") >= 2 else []:
        slot, _, item = entry.partition(":")
        if slot.isdigit() and int(slot) < 19:
            items[int(slot)] = num(item)
    return {"type": "static", "race": num(pick[2]), "class": num(pick[1]), "gender": num(pick[3]),
            "look": [num(v) for v in (pick[6].split(",") + ["0"] * 5)[:5]], "items": items}


class DpsMeter:
    """Damage per second from AZSRUN `dealt` over the last ~3 s of run time `t`."""
    WINDOW_S = 3.0

    def __init__(self):
        self.samples = []  # (t, dealt)

    def update(self, run, nopause=False):
        t, dealt = (numbers(run.get("t")) or [0.0])[0], (numbers(run.get("dealt")) or [0.0])[0]
        if self.samples and t < self.samples[-1][0]:
            self.samples = []  # a new run
        self.samples.append((t, dealt))
        while len(self.samples) > 2 and self.samples[1][0] <= t - self.WINDOW_S:
            self.samples.pop(0)
        if run.get("ph") not in (IN_RUN if nopause else ("playing",)):  # paused, choosing or dead: nothing dealt
            return 0.0
        t0, d0 = self.samples[0]
        return max(0.0, (dealt - d0) / (t - t0)) if t - t0 >= 0.5 else 0.0


def local_hero(game, dps=0.0, nopause=False):
    """Our live hero for the partner from AZSRUN: position, phase, level, dps and flags (no `paused`
    flag when co-op runs without pauses: the hero fights on while a pick or the menu is open)."""
    run = (game or {}).get("AZSRUN", (0, {}))[1]
    coord = lambda k: (numbers(run.get(k)) or [0.0])[0]
    phase = str(run.get("ph") or "") if game else ""
    paused = phase in ("paused", "choosing") and not nopause
    flags = (1 if dps > 0 else 0) | (2 if phase == "dead" else 0) | (4 if paused else 0)
    return {"x": coord("px"), "y": coord("py"), "z": coord("pz"), "facing": coord("pf"), "phase": phase,
            "lvl": int(coord("lvl")), "dps": round(dps, 1), "flags": flags, "summons": [],
            "hp": max(0, round(coord("hp"))), "hpm": max(0, round(coord("hpm")))}


MAX_PICKS_PER_FRAME = 3


def new_picks(seen, now):
    """(picks to grant, new baseline) from the partner's running pick counts: nothing on the first
    sight or when a count went down (the partner's game restarted)."""
    if not isinstance(now, list) or len(now) != 2 or not all(type(n) is int and n >= 0 for n in now):
        return [0, 0], seen
    if seen is None or any(n < s for n, s in zip(now, seen)):
        return [0, 0], now
    # кадры идут ~10 раз в секунду: скачок больше пары наград за кадр честный клиент не пришлёт, такой отбрасываем
    return [n - s if n - s <= MAX_PICKS_PER_FRAME else 0 for n, s in zip(now, seen)], now


def clock_step(gap):
    """The guest's run clock correction for a gap (the host's clock minus mine, s): none within
    CLOCK_TOLERANCE_S, the whole gap past CLOCK_JUMP_S, else a step of at most CLOCK_STEP_S."""
    if abs(gap) < CLOCK_TOLERANCE_S:
        return 0.0
    return gap if abs(gap) > CLOCK_JUMP_S else max(-CLOCK_STEP_S, min(CLOCK_STEP_S, gap))


def link_error(e):
    """A relay error short enough for one line of the window: a refused handshake's str() carries
    every response header (it made the window as wide as the screen once the match ended)."""
    code = getattr(e, "status_code", None)
    if code:
        try:
            reason = json.loads(getattr(e, "resp_body", b"") or b"{}").get("error")
        except (ValueError, AttributeError):
            reason = None
        text = f"сервер ответил {code}" + (f": {reason}" if reason else "")
    else:
        text = (str(e) or type(e).__name__).splitlines()[0]
    return text if len(text) <= 80 else text[:79] + "…"


def reconnect_delay(failures):
    """Bounded exponential reconnect pause, starting at one second."""
    return min(8, 2 ** max(0, int(failures)))


def relay_phase(run_state):
    """Wire phase corresponding to the phase byte published by the co-op exe."""
    if not run_state:
        return None
    return {0: "ready", 1: "playing", 2: "choosing", 3: "paused", 4: "dead", 5: "victory"}.get(run_state[1])


def relay_send_due(now, last_sent_at):
    """Respect the server's 50 ms frame gap; a phase edge waits for this legal slot."""
    return last_sent_at is None or now - last_sent_at >= GHOST_FRAME_GAP_S


def socket_stale(now, connected_at, partner_at):
    """Probe a silent connection after a join grace, or after a previously live partner goes quiet."""
    if partner_at >= connected_at and partner_at > 0:
        return now - partner_at >= GHOST_STALE_S
    return now - connected_at >= GHOST_CONNECT_GRACE_S


class GhostLink:
    """Co-op relay thread: sends our hero up to 20 times a second over the match's WebSocket (and our looks
    every few seconds) and writes the partner into the game's ghost block. Reconnects on any error."""
    STATIC_EVERY_S = 5

    def __init__(self, match_id, token, ghost=None, feed=None, hits=None, host=True, shared=None):
        """hits: my exe's hits block reader, None when the exe or the server (match `relay` < 2) can't
        take part in the full sync: then only the old 512-byte frames go out. host: I invited, so my
        run clock leads and my partner's run starts with mine. shared: the shared enemies (a SharedSync,
        exe and relay 3 permitting; they go live once the partner's frames show it takes part)."""
        self.match_id = match_id
        self.host = host
        self.host_started = False  # guest: the host's run is going, so mine starts too
        self.host_gone = False  # guest: the host's run is over before mine began: my start is mine again
        self.adjust = (0.0, 0)  # guest: the last clock correction for my exe (seconds, seq)
        self.adjust_next = 0.0  # guest: the earliest time of the next correction
        self.gap_max, self.gap_logged = 0.0, time.monotonic()  # guest: the widest gap to the host, logged each minute
        self.partner_ran = False  # the partner's frames showed a run in this match
        self.partner_end = 0.0  # the partner's run clock at its death in this match (0: not seen)
        self.token = token
        self.ghost = ghost
        self.feed = feed
        self.ws = None
        self.connected_at = 0.0
        self.last_sent_at = None
        self.reconnect_at = 0.0
        self.reconnect_failures = 0
        self.partner = None  # latest position frame from the partner
        self.partner_static = {}  # the partner's looks, gear and nick
        self.partner_at = 0.0
        self.trail = deque(maxlen=30)  # the partner's position samples: (sender time, arrival, frame)
        self.offset = None  # arrival minus sender time of the least delayed sample, creeping up slowly
        self.run_seq, self.run_seen = None, 0.0  # my AZSRUN sample and when I first saw it
        self.visible = False
        self.dead_since = None  # when the partner's phase became dead
        self.meter = DpsMeter()
        self.hits = hits  # my hero's hits and picks (a v3 exe), relayed as `ev` / `rw`
        self.shared = shared
        self.choice_capable = bool(ghost and getattr(ghost, "choice_rva", None))
        self.choice_negotiated = False
        self.choice_peer = ChoicePeerTracker()
        self.choice_state_cache = None
        self.events = []  # the partner's hits waiting for the ghost's ring
        self.partner_picks = None  # the partner's last pick counts
        self.grants = [0, 0]  # chest and guardian picks the partner gave me so far, for the ghost block
        self.bosses = None  # the partner's bosses [(key, health fraction)], None when it doesn't stream them
        self.static_sent_at = 0.0
        self.static_sent = None
        self.last_static = None
        self.key = None
        self.partner_dead = False  # the server says the partner died in this match (set by the Session)
        self.defeat = False  # END_RUN_DEFEAT is set in our block
        self.defeat_manual = False  # same, but a v1 exe has no control word: the player ends the run
        self.defeat_done = False  # our run ended after the partner's death: never set it again
        self.offline = False  # session over: no relay, only finish the shared defeat
        self.lang = 0  # my game's language for the exe's texts, from my AZSZONE (sticky once seen)
        self.status = ""
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def run(self):
        log.info("relay %s: start (%s)", self.match_id, "host" if self.host else "guest")
        next_at = time.monotonic()
        while not self.stop.is_set():
            game = None
            try:
                game = self.feed.read() if self.feed else None
                if not self.offline:
                    self.step(game)
                    self.status = "" if self.ghost else "Без призрака: пересобери игру через ярлык MODDED"
                else:
                    self.status = ""  # the session is over: no relay is expected any more
                self.update_choice()
            except Exception as e:
                status = f"Связь с напарником: {link_error(e)}"
                if status != self.status:
                    log.warning("relay %s: %s", self.match_id, status)
                self.status = status
                self.drop(reconnect=True)
            self.update_defeat(game)
            self.lang = game_lang(game, self.lang)
            if self.ghost:
                visible = self.partner_visible()
                if visible != self.visible:
                    self.visible = visible
                    log.info("relay %s: partner %s (phase %s, %.1f s since a frame)", self.match_id,
                             "shown" if visible else "hidden", (self.partner or {}).get("phase"),
                             time.monotonic() - self.partner_at)
                self.ghost.write(self.ally() if visible else None, self.control(), self.lang, self.grants,
                                 (*self.adjust, self.partner_end), self.bosses)
                self.ghost.push_events(self.events if visible else [])
                if self.shared:
                    self.shared.write_exe(time.monotonic(), self.partner, self.partner_live())
            self.events = []
            if self.offline and not self.defeat:
                break  # session over and nothing left to finish
            next_at = max(next_at + 1 / GHOST_HZ, time.monotonic())
            self.stop.wait(next_at - time.monotonic())
        log.info("relay %s: stop", self.match_id)
        self.drop()
        if self.ghost and self.ghost.handle:  # hide our last write; never open the game just for that
            self.ghost.write(None)
        if self.ghost:
            self.ghost.close()

    def control(self):
        """The control word: shared defeat, and with a run sync exe no pauses, the guest's start held
        until the host's run goes, then started with it."""
        control = Ghost.END_RUN_DEFEAT if self.defeat else 0
        if self.shared and not self.offline:
            control |= self.shared.bits(time.monotonic(), self.partner_live())
        if self.ghost and self.ghost.sync:
            control |= Ghost.COOP_LIVE
            if not self.host and not self.offline and not self.host_gone:
                control |= Ghost.HOLD_START | (Ghost.START_NOW if self.host_started else 0)
        return control

    def partner_live(self):
        """The partner's frames are fresh and it is in a run."""
        return bool(self.partner and time.monotonic() - self.partner_at < GHOST_STALE_S
                    and self.partner.get("phase") in IN_RUN)

    def waiting_for_host(self):
        """Guest in a run sync game whose run hasn't started yet: the start is the host's."""
        return bool(self.ghost and self.ghost.sync and not self.host and not self.host_started and not self.host_gone)

    def sync_clock(self):
        """Guest: my run clock follows the host's, extrapolated from its last frame (see clock_step).
        My clock is read right here, not at the step's start: both sides of the gap are of one moment."""
        partner = self.partner or {}
        host_t = partner.get("t")
        now = time.monotonic()
        if (self.host or not self.ghost or partner.get("phase") not in IN_RUN or not isinstance(host_t, (int, float))
                or not math.isfinite(host_t) or now - self.partner_at > 1 or now < self.adjust_next):
            return
        mine = self.ghost.run_state()
        if mine is None or mine[1] not in (1, 2, 3):
            return
        now = time.monotonic()
        gap = host_t + (now - self.partner_at) - mine[0]
        self.gap_max = max(self.gap_max, abs(gap))
        if now - self.gap_logged >= 60:
            log.info("relay %s: run clock within %.2f s of the host's over the last minute", self.match_id, self.gap_max)
            self.gap_max, self.gap_logged = 0.0, now
        step = clock_step(gap)
        if not step:
            return
        if step == gap:
            log.info("relay %s: run clock jumped %+.2f s to the host's %.2f", self.match_id, gap, host_t)
        self.adjust = (step, (self.adjust[1] + 1) & 0xFFFFFFFF)
        self.adjust_next = now + (CLOCK_JUMP_HOLD_S if step == gap else CLOCK_EVERY_S)

    def update_defeat(self, game):
        """Shared defeat: set END_RUN_DEFEAT while my run goes on after the partner died, clear it
        once my run has ended. Only the server's word counts (the partner's own death in this match);
        a relayed "dead" phase may be a death screen left over from an earlier run. A partner who
        leaves ends nothing. No feed (None, no AZSRUN phase) is unknown, never a reason to clear."""
        phase = (game or {}).get("AZSRUN", (0, {}))[1].get("ph")
        if not isinstance(phase, str) or not phase:
            return
        if self.defeat or self.defeat_manual:
            if phase not in IN_RUN:
                self.defeat = self.defeat_manual = False
                self.defeat_done = True
        elif not self.defeat_done and self.partner_dead and phase in IN_RUN:
            if self.ghost and getattr(self.ghost, "version", None) == 1:
                self.defeat_manual = True  # no control word in a v1 block
            else:
                self.defeat = True

    def step(self, game):
        import websocket  # websocket-client; only co-op needs it
        now = time.monotonic()
        if self.ws is not None and socket_stale(now, self.connected_at, self.partner_at):
            self.drop(reconnect=True)
            return
        if self.ws is None:
            if now < self.reconnect_at:
                return
            url = SERVER.rstrip("/").replace("http", "ws", 1) + f"/api/matches/{self.match_id}/ws"
            self.ws = websocket.create_connection(url, timeout=1, header=[f"Authorization: Bearer {self.token}", f"User-Agent: {USER_AGENT}"])
            self.ws.settimeout(0.02)
            self.connected_at = time.monotonic()
            self.last_sent_at = None
            self.static_sent = None  # a new socket: the partner may have missed our looks
        run = (game or {}).get("AZSRUN", (0, {}))[1]
        nopause = bool(self.ghost and self.ghost.sync)
        frame = local_hero(game, self.meter.update(run, nopause) if run else 0.0, nopause)
        clock = self.ghost.run_state() if nopause else None
        actual_phase = self.current_phase(clock)
        if actual_phase is not None:
            frame["phase"] = actual_phase
        seq = (game or {}).get("AZSRUN", (None,))[0]
        if seq != self.run_seq:
            self.run_seq, self.run_seen = seq, time.monotonic()
        frame["st"] = round(self.run_seen, 3)  # the sample's time: the partner's jitter buffer orders by it
        if clock and clock[1] in (1, 2, 3, 4):  # dead too: the partner's run ends on my clock's last value
            frame["t"] = round(clock[0], 3)
        mine = self.hits.read() if self.hits and game else None
        if mine:  # a v3 exe: the real hits instead of the partner's dps estimate
            frame["ev"], frame["rw"] = cap_events(mine[0]), mine[1]
            if mine[2] is not None:
                frame["bh"] = [list(b) for b in mine[2]]
        if self.shared:
            frame.update(self.shared.outgoing(time.monotonic()))
        self.attach_choice_frame(frame)
        text = json.dumps(frame, separators=(",", ":"))
        while len(text) > FRAME_MAX and frame.get("ev"):  # the relay closes the socket on bigger frames
            frame["ev"] = cap_events(frame["ev"], len(frame["ev"]) // 2)
            text = json.dumps(frame, separators=(",", ":"))
        now = time.monotonic()
        if relay_send_due(now, self.last_sent_at):
            self.ws.send(text)
            self.last_sent_at = now
            self.send_static()
        while True:  # drain everything the partner sent since the last step
            try:
                message = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                break
            if message.get("kind") == "static":
                self.partner_static = message
            else:
                self.take_frame(message)
        self.sync_clock()

    def take_frame(self, message):
        """A position frame from the partner."""
        markers, clean_ef = unpack_choice_ef(message.get("ef"))
        if markers:
            if self.choice_capable:
                self.choice_negotiated = True
                self.choice_peer.observe(time.monotonic(), markers)
            if clean_ef != message.get("ef"):
                message = {**message, "ef": clean_ef}
        self.partner = message
        self.partner_at = time.monotonic()
        self.reconnect_failures = 0
        self.add_sample(message)
        if self.shared:
            self.shared.incoming(message, self.partner_at)
        # the host's fresh run (its clock is in the frame); hosts without one: the server's word,
        # set by the Session
        t = message.get("t")
        if message.get("phase") in IN_RUN and isinstance(t, (int, float)) and t < FRESH_HOST_T:
            self.host_started = True
        # a death after a run in this match (not a death screen left over from an earlier run)
        if message.get("phase") in IN_RUN:
            self.partner_ran, self.partner_end = True, 0.0
        elif self.partner_ran and message.get("phase") == "dead" and isinstance(t, (int, float)) and math.isfinite(t) and t > 0:
            self.partner_end = t
        # only from a run: a death screen still streams the last run's table, its dead bosses included
        self.bosses = boss_health(message.get("bh")) if message.get("phase") in IN_RUN else None
        if isinstance(message.get("ev"), list):
            self.events = (self.events + message["ev"])[-Ghost.EV_RING:]
            granted, self.partner_picks = new_picks(self.partner_picks, message.get("rw"))
            if self.bosses is not None and self.ghost and self.ghost.boss:
                granted[1] = 0  # both exes share the guardians' health: each kill rewards on both screens
            if self.shared and self.shared.on:
                granted[0] = 0  # shared enemies: each elite dies and drops its chest on both screens
            self.grants = [g + n for g, n in zip(self.grants, granted)]

    def send_static(self):
        """Our looks and gear from the save the game runs on, on change and every few seconds.
        A save that can't be read (being written, damaged) keeps the last data, never the socket down."""
        if time.monotonic() - self.static_sent_at < self.STATIC_EVERY_S and self.static_sent is not None:
            return
        self.static_sent_at = time.monotonic()
        try:
            self.key = self.key or save.read_key()
            path = save.PVP_SAVE if save.PVP_SAVE.exists() else save.SAVE
            static = hero_static(save.load(self.key, path)) or self.last_static
        except Exception:
            static = self.last_static
        if static:
            self.ws.send(json.dumps(static))
            self.static_sent = self.last_static = static

    def attach_choice_frame(self, frame):
        """Advertise native choice capability using reserved records in the existing ef field."""
        if not self.choice_capable:
            return
        state = self.ghost.choice_state()
        if state is None:
            self.choice_state_cache = None
            return
        self.choice_state_cache = state
        base = frame.get("ef") if self.shared and not self.host else EF_HEAD.pack(0)
        frame["ef"] = pack_choice_ef(state["serial"], round(state["remaining"] * 1000), base)

    def update_choice(self):
        """Write only fresh peer evidence into the native shield block; negotiation survives disconnects."""
        if not self.choice_capable:
            return
        state = self.ghost.choice_state()
        if state is None:
            if self.shared and self.shared.host:
                self.shared.choice = {"host_shield": False, "guest_shield": False, "both": False}
            return
        now = time.monotonic()
        partner = self.partner or {}
        live = not self.offline and self.partner_live()
        phase = partner.get("phase") if live else None
        choosing = phase == "choosing"
        peer_shield = bool(choosing and self.choice_peer.active(now))
        peer_flags = (4 if live else 0) | (1 if choosing else 0) | (2 if peer_shield else 0)
        peer_remaining = max(0.0, self.choice_peer.deadline - now) if peer_shield else 0.0
        self.ghost.write_choice(self.choice_negotiated and not self.offline, peer_flags,
                                self.choice_peer.serial or 0, peer_remaining)
        own_flags = state["flags"]
        if self.shared and self.shared.host:
            self.shared.choice = {
                "host_shield": bool(self.choice_negotiated and live and own_flags & 2),
                "guest_shield": bool(self.choice_negotiated and peer_shield),
                "both": bool(self.choice_negotiated and
                             (own_flags & 4 or own_flags & 1 and choosing and live)),
            }

    def add_sample(self, message):
        """A new position sample for the jitter buffer; a frame repeating the last sample (same sender
        time) only refreshes its other fields. Senders without a clock: every frame is a sample."""
        now = time.monotonic()
        st = message.get("st")
        key = st if isinstance(st, (int, float)) else now
        if self.trail and key < self.trail[-1][0] - 1:
            self.trail.clear()  # the partner's clock went back (another PC or companion): start over
            self.offset = None
        if self.trail and key <= self.trail[-1][0]:
            self.trail[-1] = (self.trail[-1][0], self.trail[-1][1], message)
            return
        self.trail.append((key, now, message))
        # drops at once to a faster sample, rises 20 ms a second: the shown time never jumps back
        self.offset = now - key if self.offset is None else min(now - key, self.offset + OFFSET_CREEP)

    def playout(self):
        """The partner PLAYOUT_S behind its newest sample, the position interpolated between samples on
        the sender's clock, so the exe gets steady steps whatever the network does."""
        if not self.trail:
            return self.partner
        t = time.monotonic() - self.offset - PLAYOUT_S
        samples = list(self.trail)
        for (t0, _, a), (t1, _, b) in zip(samples, samples[1:]):
            if t0 <= t < t1:
                if t1 - t0 > 1:
                    return b  # the feed stalled (a load, a teleport): snap, don't slide across the map
                k = (t - t0) / (t1 - t0)
                return {**b, **{c: a[c] + (b[c] - a[c]) * k for c in ("x", "y", "z")}}
        return samples[0][2] if t < samples[0][0] else samples[-1][2]

    def ally(self):
        """The partner for the ghost block: looks and gear, then the live frame on top (flag 8: it
        streams real hits)."""
        ally = {**{k: v for k, v in self.partner_static.items() if k != "kind"}, **(self.playout() or {})}
        if "ev" in ally:
            ally["flags"] = int(ally.get("flags") or 0) | Ghost.FLAG_EVENTS
        return ally

    def partner_visible(self):
        """In a run, or dead for a moment (the exe shows the fall); hidden after DEAD_SHOW_S or when silent."""
        if self.partner is None or time.monotonic() - self.partner_at >= GHOST_STALE_S:
            return False
        if self.partner.get("phase") != "dead":
            self.dead_since = None
            return self.partner.get("phase") in IN_RUN
        self.dead_since = self.dead_since or time.monotonic()
        return time.monotonic() - self.dead_since < DEAD_SHOW_S

    def drop(self, reconnect=False):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None
        # A failed socket is definitive evidence that peer-driven effects must stop now;
        # preserve capability negotiation so the next connection need not renegotiate.
        self.partner = None
        self.partner_at = 0.0
        self.choice_peer = ChoicePeerTracker()
        if self.shared and self.shared.host:
            self.shared.choice = {"host_shield": False, "guest_shield": False, "both": False}
        if self.choice_capable and self.ghost and getattr(self.ghost, "handle", None) is not None:
            try:
                self.ghost.write_choice(self.choice_negotiated and not self.offline, 0, 0, 0.0)
            except Exception:
                pass  # the normal game-memory refresh/reconnect path will retry safely
        if reconnect:
            self.reconnect_at = time.monotonic() + reconnect_delay(self.reconnect_failures)
            self.reconnect_failures += 1
            self.last_sent_at = None

    def current_phase(self, run_state=None):
        """Read the live phase byte, bypassing the slower AZSRUN feed's phase sample."""
        if run_state is None:
            run_state = self.ghost.run_state() if self.ghost else None
        return relay_phase(run_state)

    def close(self):
        self.stop.set()
        # Clear companion-owned inputs only through an already-open game handle.
        if self.choice_capable and self.ghost and getattr(self.ghost, "handle", None) is not None:
            try:
                self.ghost.write_choice(False, 0, 0, 0.0)
            except Exception:
                pass


def press_escape_in_game():
    """Unpauses the game when the pause budget is over; only if the game has focus.
    Sends the Esc scan code, since the game may read scan codes rather than virtual keys."""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 256)
    if buf.value != GAME_TITLE:
        return False
    keyeventf_scancode, keyeventf_keyup = 0x8, 0x2
    keys = (INPUT * 2)(INPUT(type=1, ki=KEYBDINPUT(wScan=0x01, dwFlags=keyeventf_scancode)),
                       INPUT(type=1, ki=KEYBDINPUT(wScan=0x01, dwFlags=keyeventf_scancode | keyeventf_keyup)))
    user32.SendInput(2, keys, ctypes.sizeof(INPUT))
    return True


# ---------- server ----------

class ApiError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def api(method, path, body=None, token=None):
    req = urllib.request.Request(SERVER.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", e.reason)
        except Exception:
            message = e.reason
        raise ApiError(message, status=e.code) from None


def load_config(path):
    """pvp.json (nick, token, merged matches); falls back to the .bak copy if it is damaged."""
    for candidate in (path, path.with_name(path.name + ".bak")):
        try:
            return json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    if path.exists():
        raise ValueError(f"{path} повреждён, и копии нет. Удали его, чтобы зарегистрироваться заново")
    return {}


def save_config(cfg, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    tmp.replace(path)


def single_instance():
    """Holds a named mutex for the companion's lifetime; None if another one is running."""
    handle = k32.CreateMutexW(None, False, "Local\\AzsPvpCompanion")
    if not handle or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def opened_zones():
    """How many zones are open (account-wide with the mod), read from the save."""
    return max(1, save.opened_count(save.load(save.read_key(), save.SAVE).split("\n")))


def check_ready_for_pvp():
    """Raises a hint in Russian when this PC can't play a match yet."""
    if not FEED_INFO.exists():
        raise ValueError("Сначала запусти игру через ярлык MODDED, чтобы собрать exe с модом")
    if not any(l.startswith("pick|") for l in save.load(save.read_key(), save.SAVE).split("\n")):
        raise ValueError("Сыграй хотя бы один забег: в сейве ещё нет выбранного героя")


def ghost_rva(key="ghost_rva"):
    """RVA of the exe's co-op ghost block (or the hits block), or None for an exe built without it."""
    try:
        return json.loads(FEED_INFO.read_text()).get(key)
    except (OSError, ValueError):
        return None


def match_preset(match):
    """Run Options of the match as [density, health, damage, speed] percents."""
    preset = match.get("preset") or DEFAULT_PRESET
    return [int(preset[k]) for k in PRESET_ORDER]


def numbers(value):
    return [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", str(value or ""))]


def boss_time(zone):
    """Final boss arrival in run seconds from AZSZONE `len`, or None when it's missing."""
    values = numbers(zone.get("len"))
    return values[0] if values and values[0] > 0 else None


def run_options(zone):
    """The run's options from AZSZONE `mods` when they read as 4 integer percents, else None."""
    values = numbers(zone.get("mods"))
    if len(values) == 4 and all(v.is_integer() and 100 <= v <= 500 for v in values):
        return [int(v) for v in values]
    return None


def launch_game(profile=True):
    """The modded exe, on the PvP profile for a match so the main save is never touched by it."""
    env = dict(os.environ, WOW_ROGUE_SAVE=str(save.PVP_SAVE)) if profile else None
    game_dir = HERE.parent
    return subprocess.Popen([str(game_dir / GAME_EXE)], cwd=game_dir, env=env)


def start_file(match_id):
    return save.PVP_SAVE.parent / f"{match_id}.start.txt"


def build_pvp_profile(match):
    """Writes the PvP profile from the main save (drawn zone, match Run Options), keeps
    its starting text for the merge and puts the translations next to it."""
    key = save.read_key()
    folder = save.PVP_SAVE.parent
    # the game's own backups and earlier matches' leftovers must not leak into this run
    shutil.rmtree(folder / "Saves", ignore_errors=True)
    for old in list(folder.glob("characters.*")) + list(folder.glob("*.start.txt")):
        old.unlink()
    main = save.share_talent_xp(save.load(key, save.SAVE))
    start = save.pvp_profile(main, match["zone"], match_preset(match))
    (folder / "lang").mkdir(parents=True, exist_ok=True)
    start_file(match["id"]).write_text(start, encoding="utf-8")
    save.dump(key, start, save.PVP_SAVE)
    for f in (HERE / "lang").glob("*.json"):
        shutil.copy2(f, folder / "lang" / f.name)


def merge_pvp_profile(match_id, zones=False):
    """Adds what the match run earned (talent xp, items, codex, stats, achievements, and in
    co-op zone progress) to the main save."""
    key = save.read_key()
    start = start_file(match_id).read_text(encoding="utf-8")
    merged = save.merge_pvp(save.load(key, save.SAVE), start, save.load(key, save.PVP_SAVE), zones)
    save.SAVE.with_name(f"characters.before-pvp-{match_id}.txt").write_bytes(save.SAVE.read_bytes())
    backups = sorted(save.SAVE.parent.glob("characters.before-pvp-*.txt"), key=lambda f: f.stat().st_mtime)
    for old in backups[:-5]:
        old.unlink()
    save.dump(key, merged, save.SAVE)


# ---------- sync loop ----------

class Session:
    """Runs on a background thread: polls the server, reads the game, reports events."""

    def __init__(self, config=CONFIG):
        self.config = config
        self.cfg = load_config(config)
        self.rejected_token = None  # Keep the old config until a new registration succeeds.
        self.lock = threading.Lock()
        self.me = None
        self.invites = []
        self.match = None
        self.match_id = None
        self.dismissed = None  # the match the player left for the lobby: a /api/me answer racing that must not bring it back
        self.error = ""
        self.game = None  # latest feed snapshot
        self.feed = Feed() if FEED_INFO.exists() else None
        self.sent = {}  # what we already told the server for the current match
        self.proc = None  # the PvP game we launched
        self.back_to_main = False  # relaunch the game on the main save once the match game is closed
        self.info = ""
        self.merge_error = ""
        self.link = None  # co-op GhostLink of the current match
        self.ending = None  # a GhostLink finishing a shared defeat after its session ended
        self.ran_in = set()  # matches in which the server saw our run going
        self.zones = None  # opened zones, re-read every 30 s
        self.zones_at = 0.0
        self.logged = {}  # the last match summary / error / wait reason written to the log

    def token(self):
        token = self.cfg.get("token")
        return token if token != self.rejected_token else None

    def update_config(self, change):
        """Re-reads pvp.json right before writing, so nothing another write did is lost."""
        cfg = load_config(self.config)
        change(cfg)
        save_config(cfg, self.config)
        self.cfg = cfg

    def loop(self):
        while True:
            try:
                self.tick()
            except Exception as e:  # never let the worker thread die silently
                self.error = str(e) or type(e).__name__
            self.log_once("error", self.error)
            time.sleep(POLL_S)

    def log_once(self, what, text):
        """Logs `text` when it differs from the last one logged under `what` (an empty text resets)."""
        if text != self.logged.get(what):
            self.logged[what] = text
            if text:
                log.info("%s: %s", what, text)

    def tick(self):
        self.error = ""  # errors found during this tick stay visible until the next one
        game = self.feed.read() if self.feed else None
        self.game = game
        if not self.token():
            if self.rejected_token:
                self.error = AUTH_ERROR
            return
        if time.monotonic() - self.zones_at > 30:
            self.zones_at = time.monotonic()
            try:
                self.zones = opened_zones()
            except (OSError, ValueError):  # no save yet: co-op hosts just can't see our zones
                self.zones = None
        # reporting our opened zones lets a co-op host offer only zones we both have
        token = self.token()
        try:
            me = api("POST", "/api/me", {"zones": self.zones}, token)
        except ApiError as e:
            if e.status != 401:
                raise
            self.rejected_token = token
            self.error = AUTH_ERROR
            return
        invites = api("GET", "/api/invites", token=self.token())
        with self.lock:
            # matchId covers a live match or our own pending invite; keep showing a match that
            # ended (finished, declined, expired) until the player goes back to the lobby
            if me.get("matchId") != self.dismissed:
                self.match_id = me.get("matchId") or self.match_id
            match_id = self.match_id
        match = api("GET", f"/api/matches/{match_id}") if match_id else None
        with self.lock:
            if self.match_id == match_id:
                self.me, self.invites, self.match = me, invites, match
            else:  # the player went back to the lobby meanwhile
                self.me, self.invites = me, invites
        if match:
            self.log_once("match", f"{match['id']} {match.get('mode')} {match['state']} " + ", ".join(
                f"{p['nick']} {p['status']} {p['kills']}" for p in match["players"]))
        self.merge_if_done(match)  # a previous match's progress first, before a new profile replaces it
        pending = self.cfg.get("profile_match")
        merged = not pending or pending in self.cfg.get("merged", []) or self.merge_error
        if self.back_to_main and merged and not game_running():
            # "back to the normal game": the match game is closed and its progress merged just above
            # (a failed merge is retried the next time no game runs)
            self.back_to_main = False
            launch_game(profile=False)
        if match and match["state"] == "starting" and self.cfg.get("profile_match") != match["id"]:
            self.start_game(match)
        if match and match["state"] in ("starting", "running"):
            self.report(match, game)
        self.keep_link(match)

    def new_feed(self):
        """The relay's own feed reader (its own process handle, read from its own thread)."""
        return Feed() if FEED_INFO.exists() else None

    def new_ghost(self):
        rva = ghost_rva()
        choice_rva = ghost_rva("choice_rva") if ghost_rva("choice_shield") == 1 else None
        return Ghost(rva, bool(ghost_rva("ghost_sync")), bool(ghost_rva("boss_sync")), choice_rva) if rva else None

    @staticmethod
    def new_hits(feed):
        rva = ghost_rva("hits_rva")
        return Hits(feed, rva, bool(ghost_rva("boss_sync"))) if feed and rva else None

    @staticmethod
    def new_shared(ghost, host):
        """The shared enemies, when my exe has them (coop_shared.py)."""
        rva, sig = ghost_rva("shared_rva"), ghost_rva("shared_sig")
        return SharedSync(SharedBlock(ghost, rva), sig, host) if ghost and rva and sig is not None else None

    def keep_link(self, match):
        """Runs the co-op relay while a co-op match is live and my run in it isn't over (the server
        refuses the relay then), stops it otherwise."""
        coop = (match and match.get("mode") == "coop" and match["state"] in ("starting", "running")
                and self.side_of(match).get("status") not in RUN_OVER)
        if match and self.side_of(match).get("status") in ("running", "paused"):
            self.ran_in.add(match["id"])
        # the server's word only, for a match we both played: our run then ends with the partner's
        if (self.link and match and match["id"] == self.link.match_id and match["id"] in self.ran_in
                and self.partner_died(match)):
            self.link.partner_dead = True
        if self.link and match and match["id"] == self.link.match_id and not self.link.host:
            host = match["players"][0].get("status")
            if host in ("running", "paused"):
                self.link.host_started = True
            elif host in RUN_OVER and not self.link.host_started:
                self.link.host_gone = True
        if match and match.get("mode") != "coop" and self.ending and not (self.ending.defeat or self.ending.defeat_manual):
            self.ending.close()  # a PvP match starts: drop an old co-op link with nothing left to finish
            self.ending = None
        if coop and (not self.link or self.link.match_id != match["id"] or not self.link.thread.is_alive()):
            self.stop_link()  # two writers must never overlap on the ghost buffer
            feed = self.new_feed()
            hits = self.new_hits(feed) if match.get("relay", 1) >= 2 else None
            host = match["players"][0]["nick"].lower() == str(self.cfg.get("nick", "")).lower()
            ghost = self.new_ghost()
            shared = self.new_shared(ghost, host) if hits and match.get("relay", 1) >= 3 else None
            self.link = GhostLink(match["id"], self.token(), ghost, feed, hits, host, shared).start()
        elif not coop and self.link:
            if self.link.partner_dead and not self.link.defeat_done and not self.link.defeat_manual:
                # the session is over but our run must still end in defeat: let the link finish that
                self.link.offline = True
                self.ending, self.link = self.link, None
            else:
                self.stop_link()

    def partner_died(self, match):
        """The partner's own death (not ours caused by theirs, not a partner who left)."""
        nick = str(self.cfg.get("nick", "")).lower()
        return any(p["nick"].lower() != nick and p["status"] == "died" and p.get("reason") != "partner"
                   for p in match["players"])

    def stop_link(self):
        for link in (self.ending, self.link):
            if link:
                link.close()
                link.thread.join(2)
        self.ending = self.link = None

    def start_game(self, match):
        if game_running():
            # the normal game is closed for the player, but never in the middle of a run
            if self.game is None or self.game.get("AZSRUN", (0, {}))[1].get("ph") in IN_RUN:
                self.error = "Выйди из забега в игре или закрой её: матч запустит игру сам"
                return
            if not close_game():
                self.error = "Не получилось закрыть игру: закрой её сам, матч запустит её с отдельным профилем"
                return
            self.merge_if_done(match)  # the closed game may have been an earlier match's one
        # merge_if_done ran just before this in tick(); a profile it couldn't merge must never be
        # wiped by the new match's one
        pending = self.cfg.get("profile_match")
        if (pending and pending != match["id"] and pending not in self.cfg.get("merged", [])
                and start_file(pending).exists()):
            self.error = ("Прогресс прошлого матча ещё не перенесён в основной сейв, поэтому игра "
                          "для нового не запускается. Причина ниже")
            return
        build_pvp_profile(match)
        self.update_config(lambda cfg: cfg.update(profile_match=match["id"], profile_mode=match.get("mode", "pvp")))
        self.proc = launch_game()

    def relaunch(self):
        """Starts the PvP game again on the existing profile (after a crash or an early close)."""
        if not game_running():
            self.proc = launch_game()

    def merge_if_done(self, match):
        """Merges a finished match's profile once the PvP game is closed. Errors are shown in the
        window and never stop reporting the current match."""
        try:
            self.cfg = load_config(self.config)
            match_id = self.cfg.get("profile_match")
            if not match_id or match_id in self.cfg.get("merged", []) or game_running():
                return
            if match and match["id"] != match_id and match["state"] in ("starting", "running", "finished"):
                pass  # one match at a time: a newer match means ours is over, no need to ask the server
            else:
                ours = match if match and match["id"] == match_id else api("GET", f"/api/matches/{match_id}")
                # my run is what the profile holds: once it's over (left, died, timed out) and the
                # game is closed, merge without waiting for the partner
                if ours["state"] != "finished" and self.side_of(ours).get("status") not in RUN_OVER:
                    return
            # recorded first: a crash right after the dump loses one match's progress, never doubles it
            self.update_config(lambda cfg: cfg.setdefault("merged", []).append(match_id))
            try:
                merge_pvp_profile(match_id, zones=self.cfg.get("profile_mode") == "coop")
            except Exception:
                self.update_config(lambda cfg: cfg["merged"].remove(match_id))
                raise
            self.update_config(lambda cfg: [cfg.pop(k, None) for k in ("profile_match", "profile_mode")])
            start_file(match_id).unlink(missing_ok=True)
            self.merge_error = ""
            self.info = "Прогресс матча перенесён в основной сейв"
        except Exception as e:
            self.merge_error = f"Прогресс прошлого матча пока не перенесён: {e or type(e).__name__}"

    def my_side(self, match):
        return next(p for p in match["players"] if p["nick"].lower() == self.me["nick"].lower())

    def side_of(self, match):
        """My player entry by the configured nick ({} if absent); works before /api/me answered."""
        nick = str(self.cfg.get("nick", "")).lower()
        return next((p for p in match["players"] if p["nick"].lower() == nick), {})

    def report(self, match, game):
        if self.sent.get("match") != match["id"]:
            self.sent = {"match": match["id"]}
        mine = self.my_side(match)
        if game is None or "AZSRUN" not in game:
            return
        run = game["AZSRUN"][1]
        phase, kills = run.get("ph"), run.get("kills", 0)
        end_seq, end = game.get("AZSEND", (0, {}))
        zone_seq, zone = game.get("AZSZONE", (0, {}))
        last_phase, self.sent["phase"] = self.sent.get("phase"), phase
        # lines older than the match belong to an earlier run
        self.sent.setdefault("zone_base", zone_seq)
        def post(event):
            if event["type"] != "progress":
                log.info("match %s: send %s", match["id"], event)
            return api("POST", f"/api/matches/{match['id']}/event", event, self.token())

        # AZSZONE: zone=;zname=;zsub=;diff=;len=<final boss arrival, run seconds>;...;mods=<Run Options>
        zone_fresh = zone_seq != self.sent["zone_base"]
        if mine["status"] == "waiting":
            if self.cfg.get("profile_match") != match["id"]:
                # a game left open from an earlier match runs its profile: only start_game's counts
                self.log_once("wait", f"{match['id']}: the running game wasn't started for this match")
                return
            # a co-op guest's run starts with the host's, its clock jumped to the host's time
            with_host = (match.get("mode") == "coop" and self.link is not None
                         and self.link.match_id == match["id"] and self.link.host_started)
            fresh = phase == "playing" and (last_phase == "ready" or (kills == 0 and run_time(run) < 15) or with_host)
            if not fresh:
                self.log_once("wait", f"{match['id']}: not a fresh run: phase {phase}, before {last_phase}, "
                                      f"kills {'some' if kills else 0}, with host {with_host}")
                return
            if zone_fresh and zone.get("zone") and zone["zone"] != match["zone"]:
                self.error = f"Не та зона: нужна {ZONE_NAMES.get(match['zone'], match['zone'])}"
                return
            preset, options = match_preset(match), run_options(zone)
            if zone_fresh and options and options != preset:
                self.error = ("Не та сложность забега: нужно " + preset_text(preset)
                              + ". Выйди из забега и запусти игру из окна онлайна")
                return
            if zone_fresh and zone.get("mods") and not options and not self.sent.get("mods_warned"):
                self.sent["mods_warned"] = True
                self.info = "Не удалось проверить сложность забега: проверь её сам в Run Options"
            post({"type": "start"})
            self.sent.update(end_base=end_seq, kills=0, boss_at=boss_time(zone) if zone_fresh else None)
            return
        if mine["status"] not in ("running", "paused"):
            return
        # a companion restarted mid-match has no baselines: trust the lines it sees now
        new_end = end_seq != self.sent.setdefault("end_base", end_seq)
        if match.get("mode") == "coop":
            self.report_coop(run, end if new_end else None, phase, kills, post)
            return
        if "boss_at" not in self.sent or (self.sent["boss_at"] is None and zone_fresh):
            self.sent["boss_at"] = boss_time(zone)
        if self.sent["boss_at"] and run_time(run) >= self.sent["boss_at"]:
            # kills count only until the final boss arrives: report the last count from before it
            post({"type": "finished", "kills": self.sent.get("kills", 0), "reason": "boss"})
            return
        if kills != self.sent.get("kills"):
            post({"type": "progress", "kills": kills})
            self.sent["kills"] = kills
        if new_end or phase in ("dead", "victory"):
            final = end.get("kills", kills) if new_end else kills
            won = end.get("result") == "victory" if new_end else phase == "victory"
            post({"type": "finished" if won else "died", "kills": final})
            return
        if mine.get("mustResume"):
            # the server already counts us as playing: unpause the game, then clear the flag
            if phase == "paused":
                press_escape_in_game()
            else:
                post({"type": "resume"})
        elif phase == "paused" and mine["status"] == "running" and mine["pauseLeftMs"] > 0:
            post({"type": "pause"})
        elif phase != "paused" and mine["status"] == "paused":
            post({"type": "resume"})


    def report_coop(self, run, end, phase, kills, post):
        """Co-op: live stats for the partner card, then how the run ended. No pauses, no boss window."""
        number = lambda k: (numbers(run.get(k)) or [0.0])[0]
        stats = {"kills": kills, "lvl": number("lvl"), "hp": number("hp"), "hpm": number("hpm"),
                 "t": number("t"), "ph": phase or ""}
        # unchanged stats are re-sent as a heartbeat: a paused run doesn't move them, and the server
        # times out a player it hasn't heard from
        if stats != self.sent.get("stats") or time.monotonic() - self.sent.get("stats_at", 0) >= HEARTBEAT_S:
            post({"type": "progress", **stats})
            self.sent.update(stats=stats, stats_at=time.monotonic())
        if end is not None or phase in ("dead", "victory"):
            won = end.get("result") == "victory" if end is not None else phase == "victory"
            post({"type": "finished" if won else "died", "kills": (end or {}).get("kills", kills)})


def preset_text(preset):
    density, health, damage, speed = preset
    return f"врагов {density}%, здоровье {health}%, урон {damage}%, скорость {speed}%"


def run_time(run):
    try:
        return float(run.get("t", 0))
    except ValueError:
        return 0.0


# ---------- window ----------

def fmt_ms(ms):
    s = max(0, int(ms // 1000))
    return f"{s // 60}:{s % 60:02d}"


class Window:
    def __init__(self, session):
        self.s = session
        self.root = tk.Tk()
        self.root.title("Azeroth Survivors: онлайн (PvP и кооп)")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.92)
        self.root.configure(bg="#14110c")
        self.root.geometry("+40+120")
        self.body = tk.Frame(self.root, bg="#14110c", padx=12, pady=10)
        self.body.pack(fill="both")
        self.view = None
        self.action_error = ""
        self.coop_target = None  # {nick, zones} of the player looked up for co-op
        self.coop_nick = tk.StringVar()  # survives lobby rebuilds
        self.coop_nick.trace_add("write", self.search_soon)
        self.search_job = None
        self.search_q = None
        self.searched_at = 0.0
        self.suggestions = ("", [])  # (query, players) for the co-op nick field
        self.suggest_box = None
        self.suggest_shown = None
        self.refresh()

    def label(self, text, size=10, color="#e8d9b0", bold=False):
        # wraplength: one long line (a server error, a long nick) must never stretch the window
        w = tk.Label(self.body, text=text, bg="#14110c", fg=color, font=("Segoe UI", size, "bold" if bold else ""),
                     justify="left", anchor="w", wraplength=LABEL_WRAP)
        w.pack(fill="x")
        return w

    def button(self, parent, text, command):
        b = tk.Button(parent, text=text, command=command, bg="#5a1d14", fg="#f3e6c4", activebackground="#7a2a1d",
                      relief="flat", padx=8, pady=2)
        b.pack(side="left", padx=(0, 6), pady=2)
        return b

    def clear(self):
        for w in self.body.winfo_children():
            w.destroy()

    def main_game_button(self, row):
        """Closes the match game; the worker merges its progress and starts the normal game."""
        def back():
            if not close_game():
                raise ValueError("Не получилось закрыть игру: закрой её сам и запусти обычную")
            self.s.back_to_main = True
        self.button(row, "Вернуться в обычную игру", lambda: self.background(back))

    def background(self, fn, *args):
        """Runs a server call off the UI thread; widget values must be read before, in the handler."""
        def run():
            try:
                fn(*args)
                self.action_error = ""
            except Exception as e:
                self.action_error = str(e) or type(e).__name__
        threading.Thread(target=run, daemon=True).start()

    def refresh(self):
        try:
            s = self.s
            with s.lock:
                match, invites, me = s.match if s.match_id else None, s.invites, s.me
            errors = (s.error, self.action_error, s.info, s.merge_error)
            if not s.token():
                key = ("register", errors)
            elif match:
                shown = {k: v for k, v in match.items() if k != "serverNow"}
                link = s.link or s.ending
                link = (link.status, link.defeat, link.defeat_done, link.defeat_manual,
                        link.waiting_for_host()) if link else None
                key = ("match", json.dumps(shown, sort_keys=True), errors, s.game is None, link, json.dumps(invites))
            else:
                key = ("lobby", json.dumps(invites), json.dumps(me), errors, json.dumps(self.coop_target))
            if key != self.view:
                self.view = key
                self.clear()
                {"register": self.show_register, "match": lambda: self.show_match(match, invites),
                 "lobby": lambda: self.show_lobby(invites, me)}[key[0]]()
            if key[0] == "lobby":
                if time.monotonic() - self.searched_at > 20 and not self.search_job:
                    self.search()  # the online list goes stale
                self.show_suggestions()
        finally:
            self.root.after(500, self.refresh)

    def show_error(self):
        if self.s.info:
            self.label(self.s.info, 9, "#9fd08a")
        for error in (self.s.error, self.action_error, self.s.merge_error):
            if error:
                self.label(error, 9, "#ff8a6a")

    def show_register(self):
        self.label("Онлайн: PvP и кооп", 12, bold=True)
        self.label("Придумай ник (3-16 латинских букв, цифр или _)", 9, "#b9a77c")
        entry = tk.Entry(self.body, width=22)
        entry.pack(fill="x", pady=4)
        row = tk.Frame(self.body, bg="#14110c")
        row.pack(fill="x")

        def register(nick):
            r = api("POST", "/api/players", {"nick": nick})
            self.s.update_config(lambda cfg: cfg.update(nick=r["nick"], token=r["token"]))
        self.button(row, "Зарегистрироваться", lambda: self.background(register, entry.get().strip()))
        self.show_error()

    def show_lobby(self, invites, me):
        s = self.s
        me = me or {}
        self.label(f"{me.get('nick', s.cfg.get('nick'))}  рейтинг {me.get('rating', '')}", 11, bold=True)
        self.label("Вызвать игрока по нику:", 9, "#b9a77c")
        entry = tk.Entry(self.body, width=22)
        entry.pack(fill="x", pady=4)
        row = tk.Frame(self.body, bg="#14110c")
        row.pack(fill="x")

        def invite(nick):
            check_ready_for_pvp()
            api("POST", "/api/invites", {"to": nick, "zones": opened_zones()}, s.token())
        self.button(row, "Вызвать", lambda: self.background(invite, entry.get().strip()))
        self.show_coop_lobby()
        self.show_invites(invites)
        if not s.feed:
            self.label("Нет feed: пересобери игру через ярлык MODDED", 9, "#ff8a6a")
        self.show_error()

    def show_invites(self, invites):
        """Incoming invites with accept/decline; accepting drops our own pending invite."""
        s = self.s

        def answer(match_id, verb):
            if verb == "accept":
                check_ready_for_pvp()
            body = {"zones": opened_zones()} if verb == "accept" else {}
            api("POST", f"/api/matches/{match_id}/{verb}", body, s.token())
        for inv in invites or []:
            if inv.get("mode") == "coop":
                self.label(f"Кооп от {inv['from']}: {ZONE_NAMES.get(inv['zone'], inv['zone'])}", 10, "#9fd08a")
            else:
                self.label(f"Вызов от {inv['from']}", 10, "#f0c060")
            r = tk.Frame(self.body, bg="#14110c")
            r.pack(fill="x")
            self.button(r, "Принять", lambda i=inv["matchId"]: self.background(answer, i, "accept"))
            self.button(r, "Отклонить", lambda i=inv["matchId"]: self.background(answer, i, "decline"))

    def show_coop_lobby(self):
        """Co-op: look the partner up, pick any zone, invite."""
        s = self.s
        self.label("Кооп: играть вместе в одной зоне (враги 250%, здоровье 250%)", 9, "#b9a77c")
        tk.Entry(self.body, width=22, textvariable=self.coop_nick).pack(fill="x", pady=4)
        self.suggest_box = tk.Frame(self.body, bg="#14110c")
        self.suggest_box.pack(fill="x")
        self.suggest_shown = None  # a new box: refresh() fills it
        row = tk.Frame(self.body, bg="#14110c")
        row.pack(fill="x")

        def look_up(nick):
            self.coop_target = None
            self.coop_target = api("GET", f"/api/players/{urllib.request.quote(nick)}", token=s.token())
        self.button(row, "Найти", lambda: self.background(look_up, self.coop_nick.get().strip()))
        target = self.coop_target
        if not target:
            return
        # any zone can be picked; the menu starts at the furthest one both have opened
        names = [ZONE_NAMES.get(z, z) for z in save.ZONES]
        both = min(target.get("zones") or 1, s.zones or 1, len(names))
        choice = tk.StringVar(value=names[both - 1])
        menu = tk.OptionMenu(self.body, choice, *names)
        menu.configure(bg="#2a2118", fg="#f3e6c4", activebackground="#3a2d20", relief="flat", highlightthickness=0)
        menu.pack(fill="x", pady=2)
        go = tk.Frame(self.body, bg="#14110c")
        go.pack(fill="x")

        def invite(nick, zone):
            check_ready_for_pvp()
            api("POST", "/api/invites", {"to": nick, "mode": "coop", "zone": zone, "zones": opened_zones()}, s.token())
        self.button(go, f"Позвать {target['nick']} в кооп", lambda: self.background(
            invite, target["nick"], save.ZONES[names.index(choice.get())]))

    def search_soon(self, *_):
        """Nick suggestions 0.4 s after the co-op field stops changing."""
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(400, self.search)

    def search(self):
        self.search_job = None
        self.searched_at = time.monotonic()
        q = self.search_q = self.coop_nick.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{0,16}", q):
            self.suggestions = (q, [])
            return

        def fetch():
            # its own thread, not background(): suggestions are optional and must never replace or
            # clear an action's error (that also rebuilds the lobby under the cursor)
            try:
                found = api("GET", f"/api/players?q={q}", token=self.s.token())
            except (ApiError, OSError, ValueError):
                return
            if q == self.search_q:  # a slower answer to an older query must not win
                self.suggestions = (q, found)
        threading.Thread(target=fetch, daemon=True).start()

    def show_suggestions(self):
        """Fills the suggestion box in place, so the nick field keeps its focus and cursor."""
        if self.suggest_shown == self.suggestions or not (self.suggest_box and self.suggest_box.winfo_exists()):
            return
        self.suggest_shown = q, found = self.suggestions
        for w in self.suggest_box.winfo_children():
            w.destroy()
        if found and not q:
            tk.Label(self.suggest_box, text="Сейчас в сети:", bg="#14110c", fg="#b9a77c", font=("Segoe UI", 9),
                     anchor="w").pack(fill="x")
        for p in found:
            if self.coop_target and p["nick"] == self.coop_target.get("nick"):
                continue  # already picked
            tk.Button(self.suggest_box, text=p["nick"] + ("  в сети" if p.get("online") else ""), anchor="w",
                      command=lambda p=p: self.pick_partner(p), bg="#2a2118",
                      fg="#9fd08a" if p.get("online") else "#e8d9b0", activebackground="#3a2d20",
                      relief="flat", padx=6, pady=0).pack(fill="x", pady=1)

    def pick_partner(self, p):
        self.coop_target = {k: p.get(k) for k in ("nick", "rating", "zones")}
        self.coop_nick.set(p["nick"])

    def show_coop_match(self, m, invites):
        s = self.s
        self.label(f"Кооп: {ZONE_NAMES.get(m['zone'], m['zone'])}", 12, bold=True)
        self.label(f"Сложность: {preset_text(match_preset(m))}", 9, "#b9a77c")
        link = s.link or s.ending
        waiting = link and link.waiting_for_host() and s.side_of(m).get("status") == "waiting"
        if waiting:
            host = m["players"][0]["nick"]
            self.label(f"Ждём хоста: забег начнёт {host}, твой стартует вместе с ним. Зайди в зону и жди",
                       10, "#f0c060")
        elif m["state"] == "starting" and m.get("startDeadline"):
            self.label(f"Игра запускается с зоной и сложностью кооп-сессии. Начни забег: осталось "
                       f"{fmt_ms(m['startDeadline'] - m['serverNow'])}", 10, "#f0c060")
        for p in m["players"]:
            mine = s.me and p["nick"].lower() == s.me["nick"].lower()
            live = p.get("live") or {}
            phase = PHASE_NAMES.get(live.get("ph"), STATUS_NAMES.get(p["status"], p["status"]))
            if p["status"] not in ("running", "paused"):
                phase = STATUS_NAMES.get(p["status"], p["status"])
            self.label(f"{p['nick']}{' (ты)' if mine else ''}: уровень {int(live.get('lvl') or 1)}, "
                       f"{live.get('kills', p['kills'])} фрагов", 11, "#f3e6c4", bold=True)
            self.label(f"   {phase}, здоровье {int(live.get('hp') or 0)} / {int(live.get('hpm') or 0)}", 9, "#b9a77c")
        if link and link.defeat_manual:
            self.label("Напарник погиб: забег окончен. Эта сборка игры не завершает его сама, выйди из забега",
                       11, "#ff8a6a", bold=True)
        elif (link and (link.defeat or link.defeat_done)) or s.partner_died(m):
            self.label("Напарник погиб: забег окончен", 11, "#ff8a6a", bold=True)
        if link and link.status:
            self.label(link.status, 9, "#ff8a6a")
        me = s.side_of(m)
        my_status = me.get("status")
        partner = next((p for p in m["players"] if p is not me), {})
        if my_status in ("running", "paused") and partner.get("status") in RUN_OVER:
            self.label(f"{partner['nick']} {STATUS_NAMES.get(partner['status'], partner['status'])}: "
                       f"забег продолжается без напарника", 10, "#f0c060")
        if my_status == "forfeit":
            self.label("Старт твоего забега не дошёл до сервера вовремя: твои фраги в этой сессии не считаются",
                       10, "#ff8a6a")
        row = tk.Frame(self.body, bg="#14110c")
        row.pack(fill="x")

        def leave():
            api("POST", f"/api/matches/{m['id']}/event", {"type": "leave"}, s.token())
            self.to_lobby()
        if m["state"] in ("starting", "running") and my_status not in RUN_OVER:
            self.button(row, "Выйти из коопа", lambda: self.background(leave))
        else:
            self.label("Сессия завершена" if m["state"] == "finished" else "Твой забег окончен", 11, "#f0c060", bold=True)
            self.button(row, "В лобби", self.to_lobby)
            self.main_game_button(row)
        self.button(row, "Страница сессии", lambda: webbrowser.open(f"{SERVER.rstrip('/')}/m/{m['id']}"))
        if (s.game is None and m["state"] in ("starting", "running") and my_status not in RUN_OVER
                and s.cfg.get("profile_match") == m["id"]):
            self.label("Игра не запущена", 9, "#ff8a6a")
            self.button(row, "Запустить игру", lambda: self.background(s.relaunch))
        if my_status in RUN_OVER or m["state"] == "finished":
            self.show_invites(invites)
        self.show_error()

    def show_match(self, m, invites):
        s = self.s
        row = tk.Frame(self.body, bg="#14110c")
        if m["state"] == "invited":
            self.label(f"Ждём ответа от {m['players'][1]['nick']}", 11, bold=True)
            row.pack(fill="x")
            self.button(row, "Отменить", lambda: self.background(
                lambda: api("POST", f"/api/matches/{m['id']}/decline", {}, s.token())))
            self.show_invites(invites)  # an invite from the same player must not wait behind ours
            self.show_error()
            return
        if m["state"] in ("declined", "cancelled", "expired"):
            text = {"declined": "Вызов отклонён", "cancelled": "Вызов отменён", "expired": "Вызов истёк"}
            self.label(text[m["state"]], 11, bold=True)
            row.pack(fill="x")
            self.button(row, "В лобби", self.to_lobby)
            self.show_invites(invites)
            self.show_error()
            return
        if m.get("mode") == "coop":
            self.show_coop_match(m, invites)
            return
        zone = ZONE_NAMES.get(m["zone"], m["zone"])
        self.label(f"Зона: {zone}", 12, bold=True)
        self.label(f"Сложность: {preset_text(match_preset(m))}", 9, "#b9a77c")
        if m["state"] == "starting" and m.get("startDeadline"):
            self.label(f"Игра запускается с зоной и сложностью матча. Начни забег: осталось "
                       f"{fmt_ms(m['startDeadline'] - m['serverNow'])}", 10, "#f0c060")
        self.label("Фраги считаются до прихода финального босса", 9, "#b9a77c")
        for p in m["players"]:
            mine = s.me and p["nick"].lower() == s.me["nick"].lower()
            self.label(f"{p['nick']}{' (ты)' if mine else ''}: {p['kills']} фрагов", 11, "#f3e6c4", bold=True)
            self.label(f"   {STATUS_NAMES.get(p['status'], p['status'])}, время {fmt_ms(p['elapsedMs'])} из 40:00, "
                       f"пауза {fmt_ms(p['pauseLeftMs'])}", 9, "#b9a77c")
        if m["state"] == "finished":
            self.label(self.result_text(m), 11, "#f0c060", bold=True)
        row.pack(fill="x")
        # once my run is over I'm free to go, the opponent's run is still on the match page
        free = m["state"] not in ("starting", "running") or s.side_of(m).get("status") in RUN_OVER
        if free:
            self.button(row, "В лобби", self.to_lobby)
            self.main_game_button(row)
        self.button(row, "Страница матча", lambda: webbrowser.open(f"{SERVER.rstrip('/')}/m/{m['id']}"))
        if s.game is None and not free and s.cfg.get("profile_match") == m["id"]:
            self.label("Игра не запущена", 9, "#ff8a6a")
            self.button(row, "Запустить игру", lambda: self.background(s.relaunch))
        if free:
            self.show_invites(invites)
        self.show_error()

    def to_lobby(self):
        with self.s.lock:
            self.s.dismissed = self.s.match_id
            self.s.match_id = None
            self.s.match = None

    def result_text(self, m):
        if not m.get("winner"):
            return "Ничья"
        return f"Победил {m['winner']}"


class Online:
    """The online lobby and match without any toolkit, for the launcher's HTML window: state() is plain
    JSON-able data (the same views as Window), act() runs an action off the UI thread."""

    def __init__(self, session, mode=None):
        self.s = session
        self.mode = mode  # "coop" / "pvp" hide the other lobby section
        self.action_error = ""
        self.coop_target = None
        self.suggestions = ("", [])
        self.search_q = None
        self.busy = False

    def act(self, name, *args):
        fn = getattr(self, "do_" + name, None)
        if fn is None:
            raise ValueError(f"unknown online action {name}")
        if name in ("search", "pick", "to_lobby", "mode"):
            return fn(*args)  # local or self-contained: no error line

        def run():
            self.busy = True
            try:
                fn(*args)
                self.action_error = ""
            except Exception as e:
                self.action_error = str(e) or type(e).__name__
            finally:
                self.busy = False
        threading.Thread(target=run, daemon=True).start()

    def do_mode(self, mode):
        self.mode = mode

    def do_register(self, nick):
        r = api("POST", "/api/players", {"nick": nick.strip()})
        self.s.update_config(lambda cfg: cfg.update(nick=r["nick"], token=r["token"]))

    def do_invite(self, nick):
        check_ready_for_pvp()
        api("POST", "/api/invites", {"to": nick.strip(), "zones": opened_zones()}, self.s.token())

    def do_lookup(self, nick):
        self.coop_target = None
        self.coop_target = api("GET", f"/api/players/{urllib.request.quote(nick.strip())}", token=self.s.token())

    def do_pick(self, player):
        self.coop_target = {k: player.get(k) for k in ("nick", "rating", "zones")}

    def do_coop_invite(self, nick, zone_index):
        check_ready_for_pvp()
        api("POST", "/api/invites", {"to": nick, "mode": "coop", "zone": save.ZONES[int(zone_index)],
                                     "zones": opened_zones()}, self.s.token())

    def do_answer(self, match_id, verb):
        if verb not in ("accept", "decline"):
            raise ValueError("bad answer")
        if verb == "accept":
            check_ready_for_pvp()
        api("POST", f"/api/matches/{match_id}/{verb}", {"zones": opened_zones()} if verb == "accept" else {},
            self.s.token())

    def do_cancel_invite(self, match_id):
        api("POST", f"/api/matches/{match_id}/decline", {}, self.s.token())

    def do_leave(self, match_id):
        api("POST", f"/api/matches/{match_id}/event", {"type": "leave"}, self.s.token())
        self.do_to_lobby()

    def do_to_lobby(self):
        with self.s.lock:
            self.s.dismissed = self.s.match_id
            self.s.match_id = None
            self.s.match = None

    def do_back_to_main(self):
        if not close_game():
            raise ValueError("Не получилось закрыть игру: закрой её сам и запусти обычную")
        self.s.back_to_main = True

    def do_relaunch(self):
        self.s.relaunch()

    def do_search(self, q):
        """Nick suggestions for the co-op field; never touches the action error."""
        q = (q or "").strip()
        self.search_q = q
        if not re.fullmatch(r"[A-Za-z0-9_]{0,16}", q):
            self.suggestions = (q, [])
            return

        def fetch():
            try:
                found = api("GET", f"/api/players?q={q}", token=self.s.token())
            except (ApiError, OSError, ValueError):
                return
            if q == self.search_q:  # a slower answer to an older query must not win
                self.suggestions = (q, found)
        threading.Thread(target=fetch, daemon=True).start()

    def page_url(self, match_id):
        return f"{SERVER.rstrip('/')}/m/{match_id}"

    def state(self):
        s = self.s
        with s.lock:
            match, invites, me = s.match if s.match_id else None, s.invites, s.me
        out = {"mode": self.mode, "busy": self.busy, "info": s.info,
               "errors": [e for e in (s.error, self.action_error, s.merge_error) if e],
               "invites": [{"matchId": i["matchId"], "from": i["from"], "coop": i.get("mode") == "coop",
                            "zone": ZONE_NAMES.get(i.get("zone"), i.get("zone"))} for i in invites or []]}
        if not s.token():
            out["view"] = "register"
            return out
        if not match:
            q, found = self.suggestions
            names = [ZONE_NAMES.get(z, z) for z in save.ZONES]
            target = self.coop_target
            both = min((target or {}).get("zones") or 1, s.zones or 1, len(names))
            out.update(view="lobby", nick=(me or {}).get("nick", s.cfg.get("nick")), rating=(me or {}).get("rating"),
                       feed=bool(s.feed), target=target, zones=names, zone=both - 1,
                       suggestions={"q": q, "players": [p for p in found if not target or p["nick"] != target.get("nick")]})
            return out
        out.update(view="match", lines=[], buttons=[], show_invites=False)
        self.match_view(match, out)
        return out

    def match_view(self, m, out):
        s = self.s
        line = lambda text, tone="text": out["lines"].append({"text": text, "tone": tone})  # noqa: E731
        button = lambda action, label, *args: out["buttons"].append(  # noqa: E731
            {"action": action, "label": label, "args": list(args)})
        if m["state"] == "invited":
            line(f"Ждём ответа от {m['players'][1]['nick']}", "title")
            button("cancel_invite", "Отменить", m["id"])
            out["show_invites"] = True
            return
        if m["state"] in ("declined", "cancelled", "expired"):
            line({"declined": "Вызов отклонён", "cancelled": "Вызов отменён", "expired": "Вызов истёк"}[m["state"]], "title")
            button("to_lobby", "В лобби")
            out["show_invites"] = True
            return
        coop = m.get("mode") == "coop"
        me = s.side_of(m)
        my_status = me.get("status")
        link = s.link or s.ending
        line(f"{'Кооп' if coop else 'PvP'}: {ZONE_NAMES.get(m['zone'], m['zone'])}", "title")
        line(f"Сложность: {preset_text(match_preset(m))}", "muted")
        if coop and link and link.waiting_for_host() and my_status == "waiting":
            line(f"Ждём хоста: забег начнёт {m['players'][0]['nick']}, твой стартует вместе с ним. Зайди в зону и жди", "warn")
        elif m["state"] == "starting" and m.get("startDeadline"):
            line(f"Игра запускается с зоной и сложностью {'кооп-сессии' if coop else 'матча'}. Начни забег: осталось "
                 f"{fmt_ms(m['startDeadline'] - m['serverNow'])}", "warn")
        if not coop:
            line("Фраги считаются до прихода финального босса", "muted")
        for p in m["players"]:
            mine = s.me and p["nick"].lower() == s.me["nick"].lower()
            if coop:
                live = p.get("live") or {}
                phase = PHASE_NAMES.get(live.get("ph"), STATUS_NAMES.get(p["status"], p["status"]))
                if p["status"] not in ("running", "paused"):
                    phase = STATUS_NAMES.get(p["status"], p["status"])
                line(f"{p['nick']}{' (ты)' if mine else ''}: уровень {int(live.get('lvl') or 1)}, "
                     f"{live.get('kills', p['kills'])} фрагов", "strong")
                line(f"{phase}, здоровье {int(live.get('hp') or 0)} / {int(live.get('hpm') or 0)}", "muted")
            else:
                line(f"{p['nick']}{' (ты)' if mine else ''}: {p['kills']} фрагов", "strong")
                line(f"{STATUS_NAMES.get(p['status'], p['status'])}, время {fmt_ms(p['elapsedMs'])} из 40:00, "
                     f"пауза {fmt_ms(p['pauseLeftMs'])}", "muted")
        if coop:
            if link and link.defeat_manual:
                line("Напарник погиб: забег окончен. Эта сборка игры не завершает его сама, выйди из забега", "err")
            elif (link and (link.defeat or link.defeat_done)) or s.partner_died(m):
                line("Напарник погиб: забег окончен", "err")
            if link and link.status:
                line(link.status, "err")
            partner = next((p for p in m["players"] if p is not me), {})
            if my_status in ("running", "paused") and partner.get("status") in RUN_OVER:
                line(f"{partner['nick']} {STATUS_NAMES.get(partner['status'], partner['status'])}: "
                     f"забег продолжается без напарника", "warn")
            if my_status == "forfeit":
                line("Старт твоего забега не дошёл до сервера вовремя: твои фраги в этой сессии не считаются", "err")
            if m["state"] in ("starting", "running") and my_status not in RUN_OVER:
                button("leave", "Выйти из коопа", m["id"])
            else:
                line("Сессия завершена" if m["state"] == "finished" else "Твой забег окончен", "warn")
                button("to_lobby", "В лобби")
                button("back_to_main", "Вернуться в обычную игру")
            free = my_status in RUN_OVER or m["state"] == "finished"
        else:
            if m["state"] == "finished":
                line(f"Победил {m['winner']}" if m.get("winner") else "Ничья", "warn")
            free = m["state"] not in ("starting", "running") or my_status in RUN_OVER
            if free:
                button("to_lobby", "В лобби")
                button("back_to_main", "Вернуться в обычную игру")
        out["page"] = self.page_url(m["id"])
        if (s.game is None and m["state"] in ("starting", "running") and not free
                and s.cfg.get("profile_match") == m["id"]):
            line("Игра не запущена", "err")
            button("relaunch", "Запустить игру")
        out["show_invites"] = free


def start_session():
    """Logging and the server/relay worker; None when another online window already runs."""
    global _mutex
    _mutex = single_instance()  # held until the process exits
    if _mutex is None:
        return None
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    version = HERE / "VERSION"
    game = HERE.parent / "AzerothSurvivors.exe"
    log.info("companion start: mod %s, game exe %s bytes", version.read_text().strip() if version.exists() else "dev",
             game.stat().st_size if game.exists() else "?")
    session = Session()
    threading.Thread(target=session.loop, daemon=True).start()
    return session


def online(mode=None):
    """The online model for the launcher's window; raises when another online window is open."""
    session = start_session()
    if session is None:
        raise RuntimeError("Окно онлайна уже открыто в другом запуске мода. Закрой его.")
    return Online(session, mode)


def main():
    session = start_session()
    if session is None:
        user32.MessageBoxW(None, "Окно онлайна уже открыто.", "Azeroth Survivors: онлайн (PvP и кооп)", 0x30)
        return
    Window(session).root.mainloop()


if __name__ == "__main__":
    main()
