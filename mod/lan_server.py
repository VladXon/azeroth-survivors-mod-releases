"""Match hub for a local network (LAN, Radmin VPN, Hamachi): the protocol of server/ (the Cloudflare worker:
players, invites, matches, the co-op WebSocket relay), run by one player on their own PC; everyone connects to
http://<host's address>:47630. Stdlib only. Players are kept in lan-hub.json, matches only in memory.

Differences from the internet server: no 50 ms frame throttle (on a LAN the relay keeps every hit), the
companions stream at HZ instead of 20 Hz, matches tell them so (`hz`), and a UDP broadcast finds the hub.

python lan_server.py [--port 47630]
"""
import argparse
import base64
import hashlib
import json
import math
import os
import queue
import random
import re
import secrets
import select
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import lan_udp
import native_netcore

PORT = 47630
DISCOVERY_PORT = 47631
UDP_PORT = 47632
HZ = 60  # co-op relay rate the companions use on this hub, a CS-like tick (the internet server: 20)
MAX_PARTY = 2  # co-op players per match: the game shows one partner so far
ZONES = ["elwynn", "duskwood", "wsg", "ungoro", "ab", "feralas", "arena", "sm", "av", "brm", "aq", "naxx"]
PVP_ZONES = ZONES[7:]
K = 32
PRESETS = {"pvp": {"density": 125, "health": 125, "damage": 100, "speed": 100},
           "coop": {"density": 250, "health": 250, "damage": 100, "speed": 100}}
LIMITS = {"start": 180_000, "run": 2_400_000, "pause": 300_000, "invite": 600_000, "idle": 600_000}
DONE = ("died", "finished", "timeout", "forfeit", "left")
IN_RUN = ("playing", "choosing", "paused")
FRAME_MAX, EVENTS_MAX, BOSSES_MAX, EN_MAX, EF_MAX = 8192, 64, 8, 4096, 2048
ONLINE_MS, RECENT_MS, SEARCH_MAX = 120_000, 7 * 86_400_000, 10
SOCKET_IDLE_S = 30  # a relay socket silent this long is closed (companions stream many times a second)
NICK = re.compile(r"[A-Za-z0-9_]{3,16}")
B64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DISCOVER, FOUND = b"AZS-HUB?", b"AZS-HUB!"
HUB_FILE = Path(os.environ.get("APPDATA", ".")) / "wenilla-rogue" / "lan-hub.json"


class Fail(Exception):
    def __init__(self, status, error):
        super().__init__(error)
        self.status = status


def now_ms():
    return int(time.time() * 1000)


# sanitizing like server/src/index.ts: out-of-range fields become 0 (the exe's default), never the raw value
def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def number(v):
    return v if is_number(v) else 0


def clamp(v, lim):
    return max(-lim, min(lim, number(v)))


def int_in(v, lo, hi):
    ok = isinstance(v, int) and not isinstance(v, bool) or isinstance(v, float) and v.is_integer()
    return int(v) if ok and lo <= v <= hi else 0


def ints(v, n, hi):
    return [int_in(v[i] if isinstance(v, list) and i < len(v) else 0, 0, hi) for i in range(n)]


def is_count(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def b64(v, limit):
    return isinstance(v, str) and len(v) <= limit and B64.fullmatch(v) is not None


def relay_frame(hero, nick):
    """What the partners get of a companion's frame."""
    if hero.get("type") == "static":
        return {"kind": "static", "nick": nick, "race": int_in(hero.get("race"), 1, 8),
                "class": int_in(hero.get("class"), 1, 11), "gender": int_in(hero.get("gender"), 0, 1),
                "look": ints(hero.get("look"), 5, 255), "items": ints(hero.get("items"), 19, 2 ** 24 - 1)}
    out = {"kind": "pos", "nick": nick, "x": clamp(hero.get("x"), 1e5), "y": clamp(hero.get("y"), 1e5),
           "z": clamp(hero.get("z"), 1e5), "facing": clamp(hero.get("facing"), 10),
           "phase": str(hero.get("phase", ""))[:16], "lvl": int_in(hero.get("lvl"), 0, 1000),
           "dps": max(0, clamp(hero.get("dps"), 1e6)), "flags": int_in(hero.get("flags"), 0, 7)}
    for k, lim in (("t", 1e7), ("st", 1e9), ("pg", 1e4)):
        if is_number(hero.get(k)):  # the sender's run clock and steady clock, only from senders that have them
            out[k] = max(0, clamp(hero[k], lim))
    summons = hero.get("summons") if isinstance(hero.get("summons"), list) else []
    out["summons"] = [s for s in ({"kind": int_in(s.get("kind"), 1, 9), "dx": clamp(s.get("dx"), 100),
                                   "dy": clamp(s.get("dy"), 100)} for s in summons[:4] if isinstance(s, dict)) if s["kind"]]
    if isinstance(hero.get("ev"), list):
        events = [e if isinstance(e, list) else [] for e in hero["ev"][:EVENTS_MAX]]
        out.update(hp=int_in(hero.get("hp"), 0, 1e9), hpm=int_in(hero.get("hpm"), 0, 1e9), rw=ints(hero.get("rw"), 2, 1e6),
                   ev=[[int_in(e[0] if e else 0, 0, 2 ** 24 - 1)] + [f(e[i] if len(e) > i else 0) for i, f in enumerate((
                       lambda v: max(0, clamp(v, 1e7)), lambda v: int_in(v, 0, 1), lambda v: int_in(v, 0, 6),
                       lambda v: clamp(v, 1e5), lambda v: clamp(v, 1e5), lambda v: int_in(v, 0, 3),
                       lambda v: int_in(v, 0, 3)), 1)] for e in events])
    if isinstance(hero.get("bh"), list):
        out["bh"] = [[int_in(b[0] if b else 0, 0, 2 ** 24 - 1), max(0, clamp(b[1] if len(b) > 1 else 0, 1))]
                     for b in (b if isinstance(b, list) else [] for b in hero["bh"][:BOSSES_MAX])]
    if b64(hero.get("en"), EN_MAX):
        out["en"] = hero["en"]
    if b64(hero.get("ef"), EF_MAX):
        out["ef"] = hero["ef"]
    return out


def side(nick, zones=0, accepted=False):
    return {"nick": nick, "key": nick.lower(), "status": "waiting", "kills": 0, "startedAt": 0, "pausedTotal": 0,
            "pausedAt": 0, "endElapsed": 0, "mustResume": False, "ratingDelta": 0, "reason": None, "lastSeen": 0,
            "live": None, "zones": zones, "accepted": accepted}


# ---------- WebSocket (RFC 6455, the part websocket-client uses) ----------

def ws_frame(op, payload):
    n = len(payload)
    size = bytes([n]) if n < 126 else b"\x7e" + struct.pack(">H", n) if n < 65536 else b"\x7f" + struct.pack(">Q", n)
    return bytes([0x80 | op]) + size + payload


def ws_read(rfile):
    """(opcode, payload) of the next message, None once the socket is done.
    shortcut: a control frame between fragments of a message drops the message; websocket-client never fragments."""
    message, opcode = b"", None
    while True:
        head = rfile.read(2)
        if len(head) < 2:
            return None
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack(">H", rfile.read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", rfile.read(8))[0]
        if len(message) + n > 2 * FRAME_MAX:
            return 1, b"x" * (FRAME_MAX + 1)  # never buffered: the caller closes with 1009
        mask = rfile.read(4) if head[1] & 0x80 else b""
        data = rfile.read(n)
        if len(data) < n:
            return None
        if mask:
            data = (int.from_bytes(data, "big") ^ int.from_bytes((mask * (n // 4 + 1))[:n], "big")).to_bytes(n, "big")
        op = head[0] & 0x0F
        if op >= 8:
            return op, data
        opcode = opcode or op
        message += data
        if head[0] & 0x80:
            return opcode, message


class Conn:
    """A relay socket: frames go out through a queue on their own thread, so a slow partner never holds
    up the sender; one that falls 64 frames behind is closed (its companion reconnects)."""

    def __init__(self, sock, key, nick, match_id):
        self.sock, self.key, self.nick, self.match_id = sock, key, nick, match_id
        self.out = queue.Queue(maxsize=64)
        self.closed = False
        self.udp_channel = None
        self.udp_token = None
        self.udp_address = None
        self.udp_seen = self.udp_sent = 0.0
        threading.Thread(target=self.writer, daemon=True).start()

    def send(self, data, op=1):
        if self.closed:
            return
        try:
            self.out.put_nowait(ws_frame(op, data.encode() if isinstance(data, str) else data))
        except queue.Full:
            self.close(1008, "too slow")

    def close(self, code=1000, reason=""):
        if self.closed:
            return
        self.closed = True
        try:
            self.out.put_nowait(ws_frame(8, struct.pack(">H", code) + reason.encode()))
            self.out.put_nowait(None)
        except queue.Full:
            self.shutdown()

    def shutdown(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def writer(self):
        while (data := self.out.get()) is not None:
            try:
                self.sock.sendall(data)
            except OSError:
                break
        self.shutdown()


# ---------- the hub (server/src/index.ts, matches for up to MAX_PARTY players) ----------

class Hub:
    def __init__(self, path=HUB_FILE, limits=None, presets=None, max_party=MAX_PARTY):
        self.path = path
        self.lim = {**LIMITS, **(limits or {})}
        self.presets = {**PRESETS, **(presets or {})}
        self.max_party = max_party
        self.lock = threading.RLock()
        self.players = {}  # key (the lowercased nick) -> {key, nick, token (its SHA-256), rating, zones, seen}
        self.matches = {}
        self.conns = {}  # match id -> [Conn]
        self.udp_sock = None
        self.udp_sessions = {}
        try:
            self.players = {k: {**p, "key": k} for k, p in json.loads(path.read_text(encoding="utf-8")).items()} if path else {}
        except (OSError, ValueError, AttributeError):
            pass

    def store(self):
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({k: {f: p[f] for f in ("nick", "token", "rating")} for k, p in self.players.items()}),
                       encoding="utf-8")
        tmp.replace(self.path)

    def handle(self, method, path, query, body, token):
        parts = [s for s in path.split("/") if s]
        with self.lock:
            now = now_ms()
            if (method, parts) == ("POST", ["api", "players"]):
                return self.register(body.get("nick"))
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "matches"]:
                return self.view(self.load(parts[2], now), now)
            me = self.auth(token)
            if parts == ["api", "me"]:
                if method == "POST" and is_count(body.get("zones")):
                    me["zones"] = body["zones"]
                me["seen"] = now
                m = self.active(me["key"], now)
                return {"nick": me["nick"], "rating": me["rating"], "matchId": m["id"] if m else None}
            if (method, parts) == ("GET", ["api", "players"]):
                return self.search(me["key"], query.get("q", [""])[0], now)
            if method == "GET" and len(parts) == 3 and parts[:2] == ["api", "players"]:
                p = self.players.get(parts[2].lower()) or self.fail(404, "no such player")
                return {"nick": p["nick"], "rating": p["rating"], "zones": p.get("zones")}
            if (method, parts) == ("GET", ["api", "invites"]):
                return self.invites(me["key"], now)
            if (method, parts) == ("POST", ["api", "invites"]):
                return self.invite(me, body, now)
            if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "matches"]:
                verb = {"accept": self.accept, "decline": self.decline, "event": self.event}.get(parts[3])
                if verb:
                    return verb(me, parts[2], body, now)
        self.fail(404, "not found")

    @staticmethod
    def fail(status, error):
        raise Fail(status, error)

    def register(self, nick):
        if not isinstance(nick, str) or not NICK.fullmatch(nick):
            self.fail(400, "nick must be 3-16 chars [A-Za-z0-9_]")
        if nick.lower() in self.players:
            self.fail(409, "nick taken")
        token = secrets.token_hex(24)
        self.players[nick.lower()] = {"key": nick.lower(), "nick": nick, "token": hashlib.sha256(token.encode()).hexdigest(), "rating": 1000}
        self.store()
        return {"nick": nick, "token": token, "rating": 1000}

    def auth(self, token):
        if not token:
            self.fail(401, "missing bearer token")
        digest = hashlib.sha256(token.encode()).hexdigest()
        return next((p for p in self.players.values() if secrets.compare_digest(p["token"], digest)), None) \
            or self.fail(401, "bad token")

    def rating(self, key):
        return self.players.get(key, {}).get("rating", 1000)

    def search(self, key, q, now):
        if not re.fullmatch(r"[A-Za-z0-9_]{0,16}", q):
            self.fail(400, "q must be up to 16 chars [A-Za-z0-9_]")
        found = sorted((p for k, p in self.players.items() if k != key and q.lower() in k
                        and p.get("seen", 0) >= now - (RECENT_MS if q else ONLINE_MS)), key=lambda p: -p["seen"])
        return [{"nick": p["nick"], "rating": p["rating"], "zones": p.get("zones"), "online": p["seen"] >= now - ONLINE_MS}
                for p in found[:SEARCH_MAX]]

    def invites(self, key, now):
        out = []
        for m in list(self.matches.values()):
            if m["state"] != "invited":
                continue
            self.tick(m, now)
            me = next((s for s in m["p"][1:] if s["key"] == key and not s["accepted"]), None)
            if m["state"] == "invited" and me:
                out.append({"matchId": m["id"], "mode": m["mode"], "zone": m["zone"], "from": m["p"][0]["nick"],
                            "fromRating": self.rating(m["p"][0]["key"]), "expiresAt": m["createdAt"] + self.lim["invite"],
                            "players": [s["nick"] for s in m["p"]]})
        return out

    def invite(self, me, body, now):
        """`to`: a nick, or for co-op a list of up to max_party - 1 nicks."""
        mode = "coop" if body.get("mode") == "coop" else "pvp"
        names = body.get("to") if isinstance(body.get("to"), list) else [body.get("to")]
        targets = [self.players.get(n.lower()) if isinstance(n, str) else None for n in names]
        if not targets or None in targets:
            self.fail(404, "no such player")
        if len(targets) > (self.max_party - 1 if mode == "coop" else 1):
            self.fail(400, f"at most {self.max_party if mode == 'coop' else 2} players per match")
        if me["key"] in (t["nick"].lower() for t in targets):
            self.fail(400, "cannot invite yourself")
        if len({t["nick"].lower() for t in targets}) < len(targets):
            self.fail(400, "a player is invited twice")
        if self.active(me["key"], now):
            self.fail(409, "you already have an active match or invite")
        if any(self.live(t["nick"].lower(), now) for t in targets):
            self.fail(409, "player is in a match")
        zone = None
        if mode == "coop":
            zone = body.get("zone") if isinstance(body.get("zone"), str) else ""
            if zone not in ZONES:
                self.fail(400, "zone must be one of " + " ".join(ZONES))
        m = {"id": secrets.token_hex(6), "mode": mode, "state": "invited", "createdAt": now, "zone": zone,
             "startDeadline": 0, "winner": None, "preset": dict(self.presets[mode]),
             "p": [side(me["nick"], body["zones"] if is_count(body.get("zones")) else 0, True)] + [side(t["nick"]) for t in targets]}
        self.matches[m["id"]] = m
        return {"matchId": m["id"]}

    def accept(self, me, match_id, body, now):
        m = self.load(match_id, now)
        if m["state"] != "invited":
            self.fail(409, "invite is not pending")
        i = self.seat(m, me["key"])
        if i == 0:
            self.fail(403, "not your invite")
        if self.live(me["key"], now):
            self.fail(409, "you are in a match")
        for o in self.matches.values():  # accepting drops the acceptor's own outgoing invite
            if o["state"] == "invited" and o["p"][0]["key"] == me["key"]:
                o["state"] = "cancelled"
        m["p"][i].update(accepted=True, zones=body["zones"] if is_count(body.get("zones")) else 0)
        if all(s["accepted"] for s in m["p"]):
            if m["mode"] != "coop":
                m["zone"] = random.choice(PVP_ZONES)
            m["state"] = "starting"
            m["startDeadline"] = now + self.lim["start"]
        return self.view(m, now)

    def decline(self, me, match_id, body, now):
        m = self.load(match_id, now)
        if m["state"] != "invited":
            self.fail(409, "invite is not pending")
        m["state"] = "cancelled" if self.seat(m, me["key"]) == 0 else "declined"
        return self.view(m, now)

    def event(self, me, match_id, body, now):
        m = self.load(match_id, now)
        s = m["p"][self.seat(m, me["key"])]
        kind = body.get("type")
        if kind == "progress" and s["status"] in DONE:
            return self.view(m, now)  # late progress after a run ended is ignored
        if m["state"] not in ("starting", "running"):
            self.fail(409, f"match is {m['state']}")
        if "reason" in body and not (kind == "finished" and body["reason"] == "boss"):
            self.fail(400, 'reason must be "boss" on finished')
        kills = body.get("kills")
        if kills is not None and not is_count(kills):
            self.fail(400, "kills must be a non-negative integer")
        active = s["status"] in ("running", "paused")
        s["lastSeen"] = now
        if m["mode"] == "coop":
            if kind in ("pause", "resume"):
                return self.view(m, now)
            if kind == "progress" and active:
                s["live"] = {"kills": number(kills), "lvl": number(body.get("lvl")), "hp": number(body.get("hp")),
                             "hpm": number(body.get("hpm")), "ph": str(body.get("ph") or ""), "t": number(body.get("t"))}

        def unpause():
            if s["status"] == "paused":
                s["pausedTotal"] += now - s["pausedAt"]
            s["status"], s["mustResume"] = "running", False

        if kind == "start":
            if m["mode"] == "coop" and active:
                return self.view(m, now)  # the relay saw the run first
            if s["status"] != "waiting":
                self.fail(409, f"you are {s['status']}")
            s["status"], s["startedAt"] = "running", now
        elif kind == "pause":
            if s["status"] != "running":
                self.fail(409, f"you are {s['status']}")
            if s["pausedTotal"] >= self.lim["pause"]:
                self.fail(409, "no pause budget left")
            s.update(status="paused", pausedAt=now, mustResume=False)
        elif kind == "resume":
            if not active:
                self.fail(409, f"you are {s['status']}")
            unpause()
        elif kind == "progress":
            if not active:
                self.fail(409, f"you are {s['status']}")
            if kills is None:
                self.fail(400, "kills required")
            s["kills"] = max(s["kills"], kills)
        elif kind == "leave":
            if s["status"] in DONE:
                return self.view(m, now)
            s["endElapsed"], s["status"] = self.elapsed(s, now), "left"
        elif kind in ("died", "finished"):
            if not active:
                self.fail(409, f"you are {s['status']}")
            unpause()
            s["kills"] = max(s["kills"], kills or 0)
            s.update(endElapsed=self.elapsed(s, now), status=kind, reason=body.get("reason"))
        else:
            self.fail(400, "type must be start/pause/resume/progress/died/finished/leave")
        self.tick(m, now)
        return self.view(m, now)

    def seat(self, m, key):
        i = next((i for i, s in enumerate(m["p"]) if s["key"] == key), None)
        return self.fail(403, "not your match") if i is None else i

    @staticmethod
    def elapsed(s, now):
        if s["status"] in ("waiting", "forfeit"):
            return 0
        if s["status"] in DONE:
            return s["endElapsed"]
        return (s["pausedAt"] if s["status"] == "paused" else now) - s["startedAt"] - s["pausedTotal"]

    def tick(self, m, now):
        """Every time-based transition up to now."""
        if m["state"] == "invited" and now >= m["createdAt"] + self.lim["invite"]:
            m["state"] = "expired"
        if m["state"] not in ("starting", "running"):
            return
        coop = m["mode"] == "coop"
        for s in m["p"]:
            if s["status"] == "waiting" and now >= m["startDeadline"]:
                s["status"], s["kills"] = "forfeit", 0
            if coop:
                if s["status"] == "running" and now >= s["lastSeen"] + self.lim["idle"]:
                    s["endElapsed"], s["status"] = self.elapsed(s, now), "timeout"
                continue
            if s["status"] == "paused" and s["pausedTotal"] + now - s["pausedAt"] >= self.lim["pause"]:
                s.update(pausedTotal=self.lim["pause"], status="running", mustResume=True)
            if s["status"] == "running" and self.elapsed(s, now) >= self.lim["run"]:
                s["status"], s["endElapsed"] = "timeout", self.lim["run"]
            if s["status"] in DONE:
                s["mustResume"] = False
        if coop and any(s["status"] == "died" for s in m["p"]):  # shared defeat
            for s in m["p"]:
                if s["status"] not in DONE:
                    s.update(endElapsed=self.elapsed(s, now), status="died", reason="partner")
        if coop:
            for s in m["p"]:
                if s["status"] in DONE:
                    self.hang_up(m["id"], s["key"])
        if all(s["status"] in DONE for s in m["p"]):
            self.finish(m)
        elif all(s["status"] != "waiting" for s in m["p"]):
            m["state"] = "running"

    def finish(self, m):
        m["state"] = "finished"
        if m["mode"] == "coop":
            return self.hang_up(m["id"])
        a, b = m["p"]
        ra, rb = self.rating(a["key"]), self.rating(b["key"])
        score = 1 if a["kills"] > b["kills"] else 0 if a["kills"] < b["kills"] else 0.5
        delta = round(K * (score - 1 / (1 + 10 ** ((rb - ra) / 400))))
        a["ratingDelta"], b["ratingDelta"] = delta, -delta
        for p, d in ((a, delta), (b, -delta)):
            if p["key"] in self.players:
                self.players[p["key"]]["rating"] += d
        self.store()
        m["winner"] = None if score == 0.5 else a["nick"] if score == 1 else b["nick"]

    def load(self, match_id, now):
        m = self.matches.get(match_id) or self.fail(404, "no such match")
        self.tick(m, now)
        return m

    def live(self, key, now):
        """Starting/running match in which the player's own run isn't over."""
        for m in list(self.matches.values()):
            if m["state"] in ("starting", "running"):
                self.tick(m, now)
                me = next((s for s in m["p"] if s["key"] == key), None)
                if me and me["status"] not in DONE and m["state"] in ("starting", "running"):
                    return m
        return None

    def active(self, key, now):
        """Live match or own pending outgoing invite."""
        if m := self.live(key, now):
            return m
        for m in list(self.matches.values()):
            if m["state"] == "invited" and m["p"][0]["key"] == key:
                self.tick(m, now)
                if m["state"] == "invited":
                    return m
        return None

    def tick_all(self):
        with self.lock:
            now = now_ms()
            for m in list(self.matches.values()):
                if m["state"] in ("invited", "starting", "running"):
                    self.tick(m, now)

    def view(self, m, now):
        finished = m["state"] == "finished"
        return {
            "id": m["id"], "mode": m["mode"], "state": m["state"], "zone": m["zone"], "serverNow": now,
            "relay": 4, "hz": HZ, "lan": True, "startDeadline": m["startDeadline"] or None,
            "runLimitMs": self.lim["run"], "pauseBudgetMs": self.lim["pause"], "preset": m["preset"],
            "zones": [s["zones"] for s in m["p"]], "winner": m["winner"], "draw": finished and m["winner"] is None,
            "players": [{"nick": s["nick"], "rating": self.rating(s["key"]),
                         "ratingDelta": s["ratingDelta"] if finished else None, "status": s["status"],
                         "kills": s["kills"], "elapsedMs": self.elapsed(s, now),
                         "pauseLeftMs": self.lim["pause"] - s["pausedTotal"] - (now - s["pausedAt"] if s["status"] == "paused" else 0),
                         "mustResume": s["mustResume"], "reason": s["reason"], "live": s["live"],
                         "accepted": s["accepted"]} for s in m["p"]],
        }

    # --- relay ---
    def relay_check(self, match_id, token):
        with self.lock:
            me = self.auth(token)
            m = self.load(match_id, now_ms())
            mine = m["p"][self.seat(m, me["key"])]
            if m["mode"] != "coop":
                self.fail(409, "the relay is for co-op matches")
            if m["state"] not in ("starting", "running"):
                self.fail(409, f"match is {m['state']}")
            if mine["status"] in DONE:
                self.fail(409, f"you are {mine['status']}")
            self.hang_up(m["id"], me["key"])  # a reconnect replaces the player's older socket
            return me

    def join(self, conn):
        with self.lock:
            self.conns.setdefault(conn.match_id, []).append(conn)

    def leave(self, conn):
        with self.lock:
            conns = self.conns.get(conn.match_id, [])
            if conn in conns:
                conns.remove(conn)
            self.udp_sessions.pop(conn.udp_token, None)

    def offer_udp(self, conn):
        with self.lock:
            if self.udp_sock is None:
                return
            conn.udp_channel = lan_udp.make_channel()
            conn.udp_token = secrets.token_hex(16)
            self.udp_sessions[conn.udp_token] = conn
            conn.send(json.dumps({"kind": "udp_offer", "token": conn.udp_token,
                                  "port": self.udp_sock.getsockname()[1]}))

    def send_packet(self, conn, frame=None, messages=None):
        if hasattr(conn.udp_channel, 'packet_bytes'):
            raw = conn.udp_channel.packet_bytes(frame, conn.udp_token, messages)
        else:
            conn.udp_channel.queue(messages or [])
            raw = lan_udp.encode(conn.udp_channel.packet(frame, conn.udp_token))
        now = time.monotonic()
        if conn.udp_address and now - conn.udp_seen < 2:
            self.udp_sock.sendto(raw, conn.udp_address)
        else:
            conn.udp_address = None
            conn.send('{"kind":"udp_packet","packet":' + raw.decode('utf-8') + '}')
        conn.udp_sent = now

    def udp_message(self, conn, packet, raw=None):
        if raw is not None and hasattr(conn.udp_channel, 'feed_bytes'):
            result = conn.udp_channel.feed_bytes(raw, conn.udp_token)
            frame, messages = result['frame'], result['messages']
        else:
            frame, messages = conn.udp_channel.receive(packet)
        clean = relay_frame(frame, conn.nick) if frame else None
        if clean is not None and isinstance(frame.get('run'), str) and len(frame['run']) <= 48:
            clean['run'] = frame['run']
        events = []
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("ev"), list):
                continue
            event = relay_frame({"ev": [message["ev"]]}, conn.nick)["ev"][0]
            events.append({"st": max(0, clamp(message.get("st"), 1e9)), "ev": event,
                           **({'run': message['run']} if isinstance(message.get('run'), str) and len(message['run']) <= 48 else {})})
        if clean or events:
            self.touch(conn, clean or {})
        if conn.closed:
            return
        if clean or events:
            self.forward(conn, clean, events)
        if packet.get("f") is not None or packet.get("e"):
            self.send_packet(conn)  # packet ACKs; ACK-only packets never cause an ACK loop

    def udp_receive(self, raw, address):
        try:
            packet = lan_udp.decode(raw)
            with self.lock:
                conn = self.udp_sessions.get(packet.get("token"))
                if conn is None or conn.closed:
                    return
                if packet.get("hello") is True:
                    conn.udp_address, conn.udp_seen = address, time.monotonic()
                    self.udp_sock.sendto(lan_udp.encode({"hello": True, "token": conn.udp_token}), address)
                elif conn.udp_address == address:
                    conn.udp_seen = time.monotonic()
                    self.udp_message(conn, packet, raw)
        except (ValueError, TypeError, KeyError, OSError):
            return

    def touch(self, conn, hero):
        now = now_ms()
        m = self.matches.get(conn.match_id)
        s = next((s for s in m["p"] if s["key"] == conn.key), None) if m else None
        if s and m["state"] in ("starting", "running"):
            if s["status"] == "waiting" and str(hero.get("phase")) in IN_RUN:
                s.update(status="running", startedAt=now, lastSeen=now)
                self.tick(m, now)
            if s["status"] in ("running", "paused"):
                s["lastSeen"] = now

    def forward(self, conn, frame, events):
        for other in self.conns.get(conn.match_id, []):
            if other.key == conn.key or other.closed:
                continue
            if other.udp_channel is not None:
                self.send_packet(other, frame, events)
            elif frame or events:
                out = {**(frame or {"kind": "pos", "nick": conn.nick}),
                       **({"ev": [event["ev"] for event in events]} if events or frame and "ev" in frame else {})}
                other.send(json.dumps(out, separators=(",", ":")))

    def hang_up(self, match_id, key=None):
        for conn in list(self.conns.get(match_id, [])):
            if key is None or conn.key == key:
                conn.close(1000, "session over")

    def relay_message(self, conn, data):
        try:
            hero = json.loads(data)
        except ValueError:
            return
        if not isinstance(hero, dict):
            return
        with self.lock:
            if hero.get("type") == "udp_packet" and conn.udp_channel is not None:
                if hero.get("fallback") is True:
                    conn.udp_address = None
                try:
                    self.udp_message(conn, hero["packet"])
                except (ValueError, TypeError, KeyError, OSError):
                    conn.close(1008, "invalid reliable packet")
                return
            if hero.get("type") == "udp_shared" and conn.udp_channel is not None:
                clean = relay_frame({**hero, "ev": []}, conn.nick)
                for other in self.conns.get(conn.match_id, []):
                    if other.key != conn.key:
                        out = ({"kind": "shared", "nick": conn.nick,
                                **{k: clean[k] for k in ("en", "ef") if k in clean}}
                               if other.udp_channel else clean)
                        other.send(json.dumps(out, separators=(",", ":")))
                return
            if hero.get("type") != "static":
                now = now_ms()
                m = self.matches.get(conn.match_id)
                s = next((s for s in m["p"] if s["key"] == conn.key), None) if m else None
                if s and m["state"] in ("starting", "running"):
                    # a run the companion never reported as started still streams in-run frames: it started
                    if s["status"] == "waiting" and str(hero.get("phase")) in IN_RUN:
                        s.update(status="running", startedAt=now, lastSeen=now)
                        self.tick(m, now)
                    if s["status"] in ("running", "paused"):
                        s["lastSeen"] = now  # a paused game sends no progress, but its hero streams
                if conn.closed:
                    return
            clean = relay_frame(hero, conn.nick)
            if hero.get("type") == "static":
                for other in self.conns.get(conn.match_id, []):
                    if other.key != conn.key:
                        other.send(json.dumps(clean))
            else:
                events = [{"st": clean.get("st", 0), "ev": event} for event in clean.get("ev", [])]
                self.forward(conn, {**clean, **({"ev": []} if "ev" in clean else {})}, events)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AzerothLanHub/1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.serve("GET")

    def do_POST(self):
        self.serve("POST")

    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def serve(self, method):
        hub = self.server.hub
        url = urlsplit(self.path)
        bearer = re.fullmatch(r"Bearer (\S+)", self.headers.get("Authorization", ""))
        token = bearer.group(1) if bearer else None
        ws = re.fullmatch(r"/api/matches/([^/]+)/ws", url.path)
        if method == "GET" and ws:
            return self.websocket(hub, ws.group(1), token)
        body = {}
        if method == "POST":
            size = int(self.headers.get("Content-Length") or 0)
            if size > 65536:
                self.close_connection = True
                return self.reply(413, {"error": "body too big"})
            try:
                body = json.loads(self.rfile.read(size) or b"{}")
            except ValueError:
                pass
            body = body if isinstance(body, dict) else {}
        try:
            self.reply(200, hub.handle(method, url.path, parse_qs(url.query), body, token))
        except Fail as e:
            self.reply(e.status, {"error": str(e)})

    def websocket(self, hub, match_id, token):
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            return self.reply(426, {"error": "expected a websocket"})
        try:
            me = hub.relay_check(match_id, token)
        except Fail as e:
            return self.reply(e.status, {"error": str(e)})
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode())
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        sock = self.connection
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # a frame leaves at once, never batched
        sock.settimeout(SOCKET_IDLE_S)
        conn = Conn(sock, me["key"], me["nick"], match_id)
        hub.join(conn)
        if self.headers.get("X-Azeroth-UDP") == "1":
            hub.offer_udp(conn)
        try:
            while not conn.closed and (message := ws_read(self.rfile)):
                op, data = message
                if op == 8:
                    break
                if op == 9:
                    conn.send(data, 10)
                elif op in (1, 2):
                    if len(data) > FRAME_MAX:
                        conn.close(1009, "frame too big")
                        break
                    hub.relay_message(conn, data)
        except (OSError, ValueError, struct.error):
            pass
        finally:
            hub.leave(conn)
            conn.close()


# ---------- running and finding a hub ----------

def local_addresses():
    """This PC's IPv4 addresses (Radmin VPN's are 26.x.x.x), loopback left out."""
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    return sorted({i[4][0] for i in infos if not i[4][0].startswith("127.")}, key=lambda a: (not a.startswith("26."), a))


def answer_discovery(stop, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    while True:  # no SO_REUSEADDR: on Windows it would let two hubs share (and steal) the port
        try:
            sock.bind(("", DISCOVERY_PORT))
            break
        except OSError:  # an older hub on this PC still holds it
            if stop.wait(1):
                return sock.close()
    sock.settimeout(1)
    reply = FOUND + json.dumps({"port": port, "name": socket.gethostname()}).encode()
    while not stop.is_set():
        try:
            data, sender = sock.recvfrom(64)
            if data == DISCOVER:
                sock.sendto(reply, sender)
        except OSError:
            pass
    sock.close()


def discover(timeout=1.0):
    """Hubs on the local networks: [(address, port, PC name)]. A broadcast goes out of every interface
    (Windows sends 255.255.255.255 out of one only), so a Radmin VPN network is searched too."""
    socks = []
    for address in local_addresses() + ["0.0.0.0"]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((address, 0))
            s.sendto(DISCOVER, ("255.255.255.255", DISCOVERY_PORT))
            socks.append(s)
        except OSError:
            s.close()
    found, end = {}, time.monotonic() + timeout
    while socks and (left := end - time.monotonic()) > 0:
        for s in select.select(socks, [], [], left)[0]:
            try:
                data, (address, _) = s.recvfrom(256)
                info = json.loads(data[len(FOUND):]) if data.startswith(FOUND) else None
                if isinstance(info, dict) and is_count(info.get("port")):
                    found[address] = (address, info["port"], str(info.get("name", ""))[:64])
            except (OSError, ValueError):
                pass
    for s in socks:
        s.close()
    return sorted(found.values(), key=lambda f: (not f[0].startswith("26."), f[0]))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # a second hub on the same port must fail, not share it


def start(port=PORT, path=HUB_FILE, host="0.0.0.0", udp_port=None, **hub_options):
    """Runs a hub on background threads; returns the server (.hub, .shutdown_all())."""
    server = Server((host, port), Handler)
    server.hub = Hub(path, **hub_options)
    stop = threading.Event()
    try:
        address = (host, (UDP_PORT if port == PORT else 0) if udp_port is None else udp_port)
        if native_netcore.available():
            udp = native_netcore.UdpListener(address)
        else:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.bind(address)
        udp.settimeout(0.01)
        server.hub.udp_sock = udp
    except OSError:
        if 'udp' in locals() and udp is not None:
            udp.close()
        udp = None  # occupied/blocked UDP never prevents reliable WebSocket play

    def udp_loop():
        while not stop.is_set():
            try:
                raw, address = udp.recvfrom(lan_udp.MAX_DATAGRAM + 1)
                server.hub.udp_receive(raw, address)
            except OSError:
                pass
            with server.hub.lock:
                for conn in list(server.hub.udp_sessions.values()):
                    if not conn.closed and conn.udp_channel.pending and time.monotonic() - conn.udp_sent > 0.020:
                        try:
                            server.hub.send_packet(conn)
                        except (OSError, ValueError):
                            conn.udp_address = None

    def ticker():
        while not stop.wait(1):
            server.hub.tick_all()

    def shutdown_all():
        stop.set()
        if udp is not None:
            udp.close()
        server.shutdown()
        server.server_close()
        with server.hub.lock:
            for match_id in list(server.hub.conns):
                server.hub.hang_up(match_id)

    server.shutdown_all = shutdown_all
    if udp is not None:
        threading.Thread(target=udp_loop, daemon=True).start()
    for target, args in ((server.serve_forever, ()), (ticker, ()), (answer_discovery, (stop, server.server_address[1]))):
        threading.Thread(target=target, args=args, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    start(args.port)
    print(f"LAN hub on port {args.port}; addresses: {', '.join(local_addresses()) or 'none found'}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
