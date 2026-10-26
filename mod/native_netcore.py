"""Small ctypes boundary to the C++ co-op core. No Python code runs on its UDP receive thread."""
import ctypes
import json
import socket
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
_dll = None


def library():
    global _dll
    if _dll is not None:
        return _dll
    path = next((p for p in (HERE / 'native/netcore.dll', HERE / 'work/native-build/netcore.dll') if p.is_file()), None)
    if path is None:
        return None
    dll = ctypes.CDLL(str(path))
    dll.nc_abi.restype = ctypes.c_uint
    if dll.nc_abi() != 1:
        raise RuntimeError('Unsupported co-op native core ABI')
    dll.nc_channel_new.restype = ctypes.c_void_p
    dll.nc_channel_free.argtypes = [ctypes.c_void_p]
    dll.nc_channel_call.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
    dll.nc_channel_call.restype = ctypes.c_char_p
    dll.nc_channel_wire.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    dll.nc_channel_wire.restype = ctypes.c_char_p
    dll.nc_channel_feed.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
    dll.nc_channel_feed.restype = ctypes.c_char_p
    dll.nc_udp_open.argtypes = [ctypes.c_char_p, ctypes.c_ushort]
    dll.nc_udp_open.restype = ctypes.c_void_p
    dll.nc_udp_send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    dll.nc_udp_send.restype = ctypes.c_int
    dll.nc_udp_recv.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    dll.nc_udp_recv.restype = ctypes.c_int
    dll.nc_udp_close.argtypes = [ctypes.c_void_p]
    if hasattr(dll, 'nc_udp_bind'):
        dll.nc_udp_bind.argtypes = [ctypes.c_char_p, ctypes.c_ushort]
        dll.nc_udp_bind.restype = ctypes.c_void_p
        dll.nc_udp_port.argtypes = [ctypes.c_void_p]
        dll.nc_udp_port.restype = ctypes.c_ushort
        dll.nc_udp_send_to.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_ushort]
        dll.nc_udp_send_to.restype = ctypes.c_int
        dll.nc_udp_recv_from.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort)]
        dll.nc_udp_recv_from.restype = ctypes.c_int
    if hasattr(dll, 'nc_tick_open'):
        dll.nc_tick_open.argtypes = [ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint]
        dll.nc_tick_open.restype = ctypes.c_void_p
        dll.nc_tick_latest.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_float)]
        dll.nc_tick_latest.restype = ctypes.c_int
        dll.nc_tick_close.argtypes = [ctypes.c_void_p]
    _dll = dll
    return dll


def available():
    return library() is not None


class Channel:
    backend = 'C++'

    def __init__(self):
        self.dll = library()
        if self.dll is None:
            raise RuntimeError('Native core is not built')
        self.lock = threading.Lock()
        self.handle = self.dll.nc_channel_new()
        if not self.handle:
            raise MemoryError('Native channel allocation failed')

    def call(self, operation, value):
        data = json.dumps(value, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
        with self.lock:
            if self.handle is None:
                raise ValueError('channel is closed')
            raw = self.dll.nc_channel_call(self.handle, operation, data)
            if raw is None:
                raise RuntimeError('Native channel returned no result')
            response = json.loads(raw)
        if not response.get('ok'):
            raise ValueError(response.get('error', 'Invalid native packet'))
        return response['value']

    @property
    def pending(self):
        return range(self.call(4, {})['pending'])

    @property
    def newest(self):
        return self.call(4, {})['newest']

    def queue(self, messages):
        self.call(1, messages)

    def packet(self, frame=None, token=''):
        return self.call(2, {'frame': frame, 'token': token})

    def receive(self, packet):
        result = self.call(3, packet)
        return result['frame'], result['messages']

    def packet_bytes(self, frame=None, token='', messages=None):
        data = json.dumps({'frame': frame, 'messages': messages or []}, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')
        with self.lock:
            raw = self.dll.nc_channel_wire(self.handle, data, token.encode('ascii'))
            if raw is None:
                raise ValueError('Native packet exceeds its size/queue limit')
            return raw

    def feed_bytes(self, raw, token):
        with self.lock:
            result = self.dll.nc_channel_feed(self.handle, raw, token.encode('ascii'))
            if result is None:
                raise ValueError('Invalid native UDP packet')
            return json.loads(result)

    def close(self):
        with self.lock:
            if self.handle is not None:
                self.dll.nc_channel_free(self.handle)
                self.handle = None

    def __del__(self):
        if getattr(self, 'handle', None) is not None:
            self.close()


class UdpSocket:
    """Connected native Winsock endpoint; C++ reads immediately, Python consumes its queue."""
    def __init__(self, address):
        self.dll = library()
        if self.dll is None:
            raise RuntimeError('Native core is not built')
        ip = socket.gethostbyname(address[0])
        self.handle = self.dll.nc_udp_open(ip.encode('ascii'), address[1])
        if not self.handle:
            raise OSError('Native UDP open failed')

    def send(self, raw):
        sent = self.dll.nc_udp_send(self.handle, raw, len(raw))
        if sent < 0:
            raise OSError('Native UDP send failed')
        return sent

    def recv(self, size):
        buf = ctypes.create_string_buffer(size)
        n = self.dll.nc_udp_recv(self.handle, buf, size, 100)
        if n == 0:
            raise socket.timeout()
        if n < 0:
            raise OSError('Native UDP socket closed')
        return buf.raw[:n]

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            self.dll.nc_udp_close(handle)

    def __del__(self):
        if getattr(self, 'handle', None) is not None:
            self.close()


class TickWatcher:
    """The duplicated game handle and high-resolution polling worker belong to C++."""
    def __init__(self, process, address):
        self.dll = library()
        self.handle = self.dll.nc_tick_open(process, address, 2000)
        if not self.handle:
            raise OSError('Native tick watcher could not duplicate the game handle')

    def latest(self):
        seq, clock = ctypes.c_uint32(), ctypes.c_float()
        result = self.dll.nc_tick_latest(self.handle, ctypes.byref(seq), ctypes.byref(clock))
        return (seq.value, clock.value) if result == 1 else None

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            self.dll.nc_tick_close(handle)

    def __del__(self):
        if getattr(self, 'handle', None) is not None:
            self.close()


class UdpListener(UdpSocket):
    def __init__(self, address):
        self.dll = library()
        self.handle = self.dll.nc_udp_bind(address[0].encode('ascii'), address[1])
        if not self.handle:
            raise OSError('Native UDP bind failed')
        self.address = (address[0], self.dll.nc_udp_port(self.handle))
        self.timeout_ms = 10

    def settimeout(self, value):
        self.timeout_ms = max(1, int(value * 1000))

    def getsockname(self):
        return self.address

    def recvfrom(self, size):
        buf, ip, port = ctypes.create_string_buffer(size), ctypes.create_string_buffer(16), ctypes.c_ushort()
        n = self.dll.nc_udp_recv_from(self.handle, buf, size, self.timeout_ms, ip, ctypes.byref(port))
        if n == 0:
            raise socket.timeout()
        if n < 0:
            raise OSError('Native UDP listener closed')
        return buf.raw[:n], (ip.value.decode('ascii'), port.value)

    def sendto(self, raw, address):
        sent = self.dll.nc_udp_send_to(self.handle, raw, len(raw), address[0].encode('ascii'), address[1])
        if sent < 0:
            raise OSError('Native UDP send failed')
        return sent
