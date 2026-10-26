#define WIN32_LEAN_AND_MEAN
#include <windows.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <thread>
#include <unordered_map>

namespace {
struct Tick {
  uint32_t seq;
  uint32_t count;
  float clock;
};

struct Poller {
  HANDLE process = nullptr;
  HANDLE stop = nullptr;
  HANDLE timer = nullptr;
  uint64_t address = 0;
  unsigned interval_us = 2000;
  std::mutex mutex;
  bool has_latest = false;
  Tick latest{};
  std::thread worker;

  void run() {
    LARGE_INTEGER due{};
    due.QuadPart = -static_cast<LONGLONG>(interval_us) * 10;
    for (;;) {
      if (!SetWaitableTimer(timer, &due, 0, nullptr, nullptr, FALSE)) break;
      HANDLE waits[2] = {stop, timer};
      DWORD w = WaitForMultipleObjects(2, waits, FALSE, INFINITE);
      if (w == WAIT_OBJECT_0 || w != WAIT_OBJECT_0 + 1) break;

      Tick first{};
      SIZE_T got = 0;
      if (!ReadProcessMemory(process, reinterpret_cast<const void*>(address), &first, sizeof(first), &got) || got != sizeof(first)) continue;
      uint32_t verify = 0;
      if (!ReadProcessMemory(process, reinterpret_cast<const void*>(address), &verify, sizeof(verify), &got) || got != sizeof(verify)) continue;
      if ((first.seq & 1u) || first.seq != verify || !std::isfinite(first.clock)) continue;
      std::lock_guard<std::mutex> lock(mutex);
      latest = first;
      has_latest = true;
    }
  }

  ~Poller() {
    if (stop) SetEvent(stop);
    if (worker.joinable()) worker.join();
    if (timer) CloseHandle(timer);
    if (stop) CloseHandle(stop);
    if (process) CloseHandle(process);
  }
};

std::mutex registry_mutex;
std::unordered_map<uintptr_t, std::shared_ptr<Poller>> registry;
uintptr_t next_id = 1;

std::shared_ptr<Poller> get_poller(void* token) {
  if (!token) return {};
  std::lock_guard<std::mutex> lock(registry_mutex);
  auto it = registry.find(reinterpret_cast<uintptr_t>(token));
  return it == registry.end() ? std::shared_ptr<Poller>{} : it->second;
}
}

extern "C" __declspec(dllexport) unsigned nc_tick_abi() { return 1; }

extern "C" __declspec(dllexport) void* nc_tick_open(uint64_t process_handle, uint64_t header_address, unsigned poll_us) {
  try {
    HANDLE duplicated = nullptr;
    HANDLE borrowed = reinterpret_cast<HANDLE>(static_cast<uintptr_t>(process_handle));
    if (!borrowed || !header_address || !DuplicateHandle(GetCurrentProcess(), borrowed, GetCurrentProcess(), &duplicated, 0, FALSE, DUPLICATE_SAME_ACCESS)) return nullptr;
    auto p = std::make_shared<Poller>();
    p->process = duplicated;
    p->address = header_address;
    p->interval_us = poll_us ? poll_us : 2000;
    p->stop = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!p->stop) return nullptr;
    p->timer = CreateWaitableTimerExW(nullptr, nullptr, 0x00000002 /* CREATE_WAITABLE_TIMER_HIGH_RESOLUTION */, TIMER_MODIFY_STATE | SYNCHRONIZE);
    if (!p->timer) p->timer = CreateWaitableTimerW(nullptr, FALSE, nullptr);
    if (!p->timer) return nullptr;
    p->worker = std::thread([p] { p->run(); });

    std::lock_guard<std::mutex> lock(registry_mutex);
    uintptr_t id = next_id++;
    if (!id) id = next_id++;
    registry.emplace(id, std::move(p));
    return reinterpret_cast<void*>(id);
  } catch (...) {
    return nullptr;
  }
}

extern "C" __declspec(dllexport) int nc_tick_latest(void* token, uint32_t* seq, float* clock) {
  try {
    auto p = get_poller(token);
    if (!p) return -1;
    if (!seq || !clock) return 0;
    std::lock_guard<std::mutex> lock(p->mutex);
    if (!p->has_latest) return 0;
    *seq = p->latest.seq;
    *clock = p->latest.clock;
    return 1;
  } catch (...) {
    return 0;
  }
}

extern "C" __declspec(dllexport) void nc_tick_close(void* token) {
  try {
    if (!token) return;
    std::shared_ptr<Poller> p;
    {
      std::lock_guard<std::mutex> lock(registry_mutex);
      auto it = registry.find(reinterpret_cast<uintptr_t>(token));
      if (it == registry.end()) return;
      p = std::move(it->second);
      registry.erase(it);
    }
    SetEvent(p->stop);
    if (p->worker.joinable()) p->worker.join();
  } catch (...) {
  }
}
