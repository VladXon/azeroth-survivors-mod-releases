"""Pure Python packet codec and reliable ordered UDP channel primitive."""
import json

MAX_DATAGRAM = 1200
_U32 = 0xFFFFFFFF
_WINDOW = 4096


def make_channel():
    """Release builds use C++; source-only installations retain the reference implementation."""
    import native_netcore
    return native_netcore.Channel() if native_netcore.available() else Channel()


def encode(value):
    try:
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("value is not compact JSON") from exc
    if len(data) > MAX_DATAGRAM:
        raise ValueError("UDP datagram exceeds 1200 bytes")
    return data


def decode(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_DATAGRAM:
        raise ValueError("invalid or oversized UDP datagram")
    try:
        value = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("malformed UDP JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("packet must be a JSON object")
    return value


def _uint(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _U32:
        raise ValueError("%s must be a u32" % name)
    return value


def _newer(a, b):
    delta = (a - b) & _U32
    return 0 < delta < 0x80000000


class Channel:
    backend = 'Python'
    def __init__(self):
        self.pending = {}
        self.newest = None
        self._frame_seq = None
        self.seq = 0
        self._next_message = 0
        self._expected = 0
        self._buffer = {}
        self._ack = None
        self._bits = 0
        self._sent = {}
        self._send_cursor = 0

    def queue(self, messages):
        if not isinstance(messages, list):
            raise ValueError("messages must be a list")
        if len(self.pending) + len(messages) > _WINDOW:
            raise ValueError("reliable message queue is full")
        for message in messages:
            encode(message)
        for message in messages:
            mid = self._next_message
            self.pending[mid] = message
            self._next_message += 1

    def _process_ack(self, ack, bits):
        if ack is None:
            return
        _uint(ack, "a")
        _uint(bits, "b")
        acknowledged = {ack}
        for i in range(32):
            if bits & (1 << i):
                acknowledged.add((ack - i - 1) & _U32)
        for seq in acknowledged:
            for mid in self._sent.pop(seq, ()):
                self.pending.pop(mid, None)

    def _record_received(self, seq):
        if self._ack is None:
            self._ack, self._bits = seq, 0
        elif seq == self._ack:
            return
        elif _newer(seq, self._ack):
            shift = (seq - self._ack) & _U32
            self._bits = 0 if shift > 32 else ((self._bits << shift) | (1 << (shift - 1))) & _U32
            self._ack = seq
        else:
            behind = (self._ack - seq) & _U32
            if 1 <= behind <= 32:
                self._bits |= 1 << (behind - 1)

    def packet(self, frame=None, token=""):
        if frame is not None:
            encode(frame)
        if not isinstance(token, str):
            raise ValueError("token must be a string")
        base = {"token": token, "s": self.seq & _U32, "a": self._ack, "b": self._bits, "f": frame, "e": []}
        selected = []
        items = list(self.pending.items())
        if items:
            oldest = items[0]
            items = [oldest] + [item for item in items if item[0] >= self._send_cursor and item != oldest] \
                + [item for item in items if item[0] < self._send_cursor and item != oldest]
        for mid, payload in items:
            trial = selected + [[mid, payload]]
            candidate = dict(base, e=trial)
            try:
                encode(candidate)
            except ValueError:
                # Leave this reliable item queued and send a fitting frame alone.
                encode(base)
                break
            selected = trial
        if selected:
            self._send_cursor = selected[-1][0] + 1
        base["e"] = selected
        encode(base)
        seq = self.seq & _U32
        self._sent[seq] = tuple(item[0] for item in selected)
        if len(self._sent) > 8192:
            # Keep recent packet history; reliable messages themselves remain pending.
            for old in list(self._sent)[:len(self._sent) - 8192]:
                del self._sent[old]
        self.seq = (seq + 1) & _U32
        return base

    def receive(self, packet):
        if not isinstance(packet, dict):
            raise ValueError("packet must be an object")
        try:
            seq = _uint(packet["s"], "s")
            ack = packet["a"]
            if ack is not None:
                _uint(ack, "a")
            bits = _uint(packet["b"], "b")
            frame = packet["f"]
            entries = packet["e"]
        except (KeyError, TypeError) as exc:
            raise ValueError("malformed packet envelope") from exc
        if frame is not None:
            if not isinstance(frame, dict):
                raise ValueError("frame must be an object or null")
            encode(frame)
        if not isinstance(entries, list) or len(entries) > _WINDOW:
            raise ValueError("invalid reliable message list")
        parsed = []
        for entry in entries:
            if not isinstance(entry, list) or len(entry) != 2:
                raise ValueError("invalid reliable message entry")
            mid = _uint(entry[0], "message id")
            encode(entry[1])
            if mid >= self._expected + _WINDOW:
                raise ValueError("reliable message exceeds reorder window")
            parsed.append((mid, entry[1]))
        # Commit packet-level state only after validating every field.
        self._process_ack(ack, bits)
        self._record_received(seq)
        delivered_frame = None
        if self.newest is None or _newer(seq, self.newest):
            self.newest = seq
        if frame is not None and (self._frame_seq is None or _newer(seq, self._frame_seq)):
            self._frame_seq = seq
            delivered_frame = frame
        delivered = []
        for mid, payload in parsed:
            if mid < self._expected or mid in self._buffer:
                continue
            self._buffer[mid] = payload
        while self._expected in self._buffer:
            delivered.append(self._buffer.pop(self._expected))
            self._expected += 1
        return delivered_frame, delivered
