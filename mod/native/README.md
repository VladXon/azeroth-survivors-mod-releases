# Native co-op core

The Windows x64 C++17 DLL provides reliable ordered event transport, connected UDP endpoints,
a UDP listener, and a read-only game publication watcher. The Python launcher/session layer
uses a small C ABI through `native_netcore.py`. The original game remains a separate process.

## Build

Use `python tools/build_netcore.py`. The default development output is
`work/native-build/netcore.dll`. The builder accepts `AZS_CXX` pointing to a portable Zig
executable, or uses `work/toolchain/zig-x86_64-windows-0.15.2/zig.exe`. No system installation
or PATH change is required. `zig c++` compiles C++ and links its bundled C++ runtime.

Launcher/release packaging builds the DLL from the committed source tree and places it at
`native/netcore.dll` in the downloadable mod manifest. Generated DLLs are not committed.
Source-only installs without a built DLL retain the Python reference transport.

## Protocol and lifetime

- Maximum wire datagram: 1200 UTF-8 bytes, including the session token and envelope.
- Packet sequence and ACK use u32 with a 32-bit history mask. Position freshness is tracked
  separately from ACK-only packet freshness.
- Hits have independent ordered IDs, are retried until acknowledged and delivered once.
  The oldest pending hit is retried promptly; remaining packet space rotates through the queue,
  allowing more messages to progress before a full round trip completes.
- WebSocket fallback preserves the same channel, ACK state and message IDs. Shared enemy
  tables and choice-capability records remain on WebSocket.
- Native UDP workers own their socket and receive queue. Closing wakes blocked readers;
  shared ownership keeps endpoints alive while an API call is in progress.
- The tick watcher duplicates an existing game handle and reads a stable seq/clock header
  on a 2 ms high-resolution timer. It does not write or inject into the game.
- The game-facing damage, ownership and choice rules remain in the existing patches.

## Verification

Run `tools/test_netcore.py`, `tools/test_game_bridge.py`, `tools/test_native_backend.py`,
`tools/test_lan_udp.py`, and `tools/test_lan_server.py`. Native ghost motion is checked by
`tools/test_ghost_motion_native.py ..\AzerothSurvivors.exe` using the emitted instructions
in Unicorn. These checks do not establish visual behavior in two running games or Radmin RTT.

The native bridge avoids repeated JSON conversions on the packet path. A local CPU-only
comparison of reliable-event processing measured median 303→185 µs for 16 events and
1301→746 µs for 64 events. One event remained slower through the C ABI (41→77 µs).
These are protocol microbenchmarks, not FPS or end-to-end network gains.

## Third-party code

`vendor/json.hpp` is nlohmann/json v3.12.0, downloaded from its official repository;
SHA-256 `aaf127c04cb31c406e5b04a63f1ae89369fccde6d8fa7cdda1ed4f32dfc5de63`.
Its MIT license is `vendor/json.LICENSE.txt`. Runtime notices for libc++, libc++abi,
libunwind and MinGW are under `vendor/licenses/`.
