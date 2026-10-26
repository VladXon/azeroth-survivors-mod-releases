#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include "vendor/json.hpp"
#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <utility>
#include <vector>

using json = nlohmann::json;
namespace {
constexpr size_t MAX_DATAGRAM = 1200;
constexpr uint64_t WINDOW = 4096;
constexpr uint32_t U32 = 0xffffffffu;

bool newer(uint32_t a, uint32_t b) {
    uint32_t delta = a - b;
    return delta != 0 && delta < 0x80000000u;
}

uint32_t read_u32(const json& value, const char* name) {
    if (!value.is_number_unsigned() &&
        !(value.is_number_integer() && value.get<int64_t>() >= 0)) {
        throw std::runtime_error(std::string(name) + " must be a u32");
    }
    if (value.is_number_integer() && value.get<int64_t>() > U32) {
        throw std::runtime_error(std::string(name) + " must be a u32");
    }
    if (value.is_number_unsigned() && value.get<uint64_t>() > U32) {
        throw std::runtime_error(std::string(name) + " must be a u32");
    }
    return value.get<uint32_t>();
}

void validate_compact(const json& value) {
    try {
        auto serialized = value.dump(-1, ' ', false, json::error_handler_t::strict);
        if (serialized.size() > MAX_DATAGRAM) {
            throw std::runtime_error("UDP datagram exceeds 1200 bytes");
        }
    } catch (const std::exception&) {
        throw std::runtime_error("value is not compact JSON");
    }
}

struct Channel {
    std::mutex mutex;
    std::string raw_result;
    std::map<uint64_t, json> pending;
    uint64_t next_message = 0;
    uint64_t send_cursor = 0;
    uint64_t expected = 0;
    uint32_t sequence = 0;
    bool has_newest = false;
    bool has_frame = false;
    bool has_ack = false;
    uint32_t newest = 0;
    uint32_t frame_sequence = 0;
    uint32_t ack = 0;
    uint32_t ack_bits = 0;
    std::map<uint64_t, json> reorder_buffer;
    std::map<uint32_t, std::vector<uint64_t>> sent;

    void process_ack(const json& ack_value, uint32_t bits) {
        if (ack_value.is_null()) return;
        uint32_t ack_seq = read_u32(ack_value, "a");
        std::vector<uint32_t> acknowledged{ack_seq};
        for (unsigned bit = 0; bit < 32; ++bit) {
            if (bits & (1u << bit)) acknowledged.push_back(ack_seq - bit - 1);
        }
        for (uint32_t seq : acknowledged) {
            auto it = sent.find(seq);
            if (it == sent.end()) continue;
            for (uint64_t message_id : it->second) pending.erase(message_id);
            sent.erase(it);
        }
    }

    void record_received(uint32_t seq) {
        if (!has_ack) {
            ack = seq;
            ack_bits = 0;
            has_ack = true;
        } else if (seq == ack) {
            return;
        } else if (newer(seq, ack)) {
            uint32_t shift = seq - ack;
            if (shift >= 32) ack_bits = shift == 32 ? 0x80000000u : 0;
            else ack_bits = (ack_bits << shift) | (1u << (shift - 1));
            ack = seq;
        } else {
            uint32_t behind = ack - seq;
            if (behind >= 1 && behind <= 32) ack_bits |= 1u << (behind - 1);
        }
    }

    json make_packet(const json& input) {
        json frame = input.at("frame");
        std::string token = input.at("token").get<std::string>();
        if (!frame.is_null()) {
            if (!frame.is_object()) throw std::runtime_error("frame must be an object or null");
            validate_compact(frame);
        }
        json packet = {{"token", token}, {"s", sequence},
                       {"a", has_ack ? json(ack) : json(nullptr)}, {"b", ack_bits},
                       {"f", frame}, {"e", json::array()}};
        validate_compact(packet);

        json selected = json::array();
        size_t used_bytes = packet.dump(-1, ' ', false, json::error_handler_t::strict).size();
        std::vector<uint64_t> ids;
        std::vector<uint64_t> order;
        if (!pending.empty()) {
            auto oldest = pending.begin();
            order.push_back(oldest->first);
            auto split = pending.lower_bound(send_cursor);
            for (auto it = split; it != pending.end(); ++it)
                if (it->first != oldest->first) order.push_back(it->first);
            for (auto it = pending.begin(); it != split; ++it)
                if (it->first != oldest->first) order.push_back(it->first);
        }
        uint64_t last_selected = 0;
        bool selected_any = false;
        for (uint64_t id : order) {
            const auto& item = *pending.find(id);
            json row = json::array({item.first, item.second});
            size_t row_bytes = row.dump(-1, ' ', false, json::error_handler_t::strict).size();
            size_t required = row_bytes + (selected.empty() ? 0 : 1);
            if (used_bytes + required > MAX_DATAGRAM) break;
            used_bytes += required;
            selected.push_back(std::move(row));
            ids.push_back(item.first);
            last_selected = item.first;
            selected_any = true;
        }
        packet["e"] = selected;
        if (packet.dump(-1, ' ', false, json::error_handler_t::strict).size() > MAX_DATAGRAM)
            throw std::runtime_error("UDP datagram exceeds 1200 bytes");
        sent[sequence] = std::move(ids);
        if (selected_any) send_cursor = last_selected + 1;
        if (sent.size() > 8192) sent.erase(sent.begin());
        ++sequence;
        return packet;
    }

    json receive(const json& packet) {
        if (!packet.is_object()) throw std::runtime_error("packet must be an object");
        uint32_t seq = read_u32(packet.at("s"), "s");
        json ack_value = packet.at("a");
        if (!ack_value.is_null()) read_u32(ack_value, "a");
        uint32_t bits = read_u32(packet.at("b"), "b");
        json frame = packet.at("f");
        json entries = packet.at("e");
        if (!frame.is_null()) {
            if (!frame.is_object()) throw std::runtime_error("frame must be an object or null");
            validate_compact(frame);
        }
        if (!entries.is_array() || entries.size() > WINDOW)
            throw std::runtime_error("invalid reliable message list");

        std::vector<std::pair<uint64_t, json>> parsed;
        for (const auto& entry : entries) {
            if (!entry.is_array() || entry.size() != 2)
                throw std::runtime_error("invalid reliable message entry");
            uint32_t id = read_u32(entry[0], "message id");
            validate_compact(entry[1]);
            if (static_cast<uint64_t>(id) >= expected + WINDOW)
                throw std::runtime_error("reliable message exceeds reorder window");
            parsed.emplace_back(id, entry[1]);
        }

        process_ack(ack_value, bits);
        record_received(seq);
        if (!has_newest || newer(seq, newest)) {
            newest = seq;
            has_newest = true;
        }
        json delivered_frame = nullptr;
        if (!frame.is_null() && (!has_frame || newer(seq, frame_sequence))) {
            frame_sequence = seq;
            has_frame = true;
            delivered_frame = frame;
        }
        for (auto& item : parsed) {
            if (item.first >= expected && !reorder_buffer.count(item.first))
                reorder_buffer.emplace(item.first, item.second);
        }
        json messages = json::array();
        while (reorder_buffer.count(expected)) {
            messages.push_back(reorder_buffer[expected]);
            reorder_buffer.erase(expected++);
        }
        return {{"frame", delivered_frame}, {"messages", messages}};
    }
};

struct Datagram { std::string data; sockaddr_in peer{}; };
struct Endpoint {
    SOCKET socket = INVALID_SOCKET;
    bool bound = false;
    std::mutex mutex;
    std::condition_variable ready;
    std::deque<Datagram> queue;
    bool stopped = false;
    std::thread worker;

    void start() {
        worker = std::thread([this] {
            char buffer[65536];
            for (;;) {
                sockaddr_in peer{};
                int peer_size = sizeof(peer);
                int size = bound
                    ? recvfrom(socket, buffer, sizeof(buffer), 0,
                               reinterpret_cast<sockaddr*>(&peer), &peer_size)
                    : recv(socket, buffer, sizeof(buffer), 0);
                if (size == SOCKET_ERROR) break;
                if (size > static_cast<int>(MAX_DATAGRAM)) continue;
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    if (stopped) break;
                    if (queue.size() < 512) {
                        Datagram datagram;
                        datagram.data.assign(buffer, buffer + size);
                        datagram.peer = peer;
                        queue.emplace_back(std::move(datagram));
                    }
                }
                ready.notify_one();
            }
            std::lock_guard<std::mutex> lock(mutex);
            stopped = true;
            ready.notify_all();
        });
    }

    void stop() {
        { std::lock_guard<std::mutex> lock(mutex); stopped = true; }
        shutdown(socket, SD_BOTH);
        closesocket(socket);
        ready.notify_all();
        if (worker.joinable() && worker.get_id() != std::this_thread::get_id()) worker.join();
        socket = INVALID_SOCKET;
    }
};

std::mutex endpoint_registry_mutex;
std::unordered_map<void*, std::shared_ptr<Endpoint>> endpoints;
std::once_flag winsock_once;

std::shared_ptr<Endpoint> get_endpoint(void* handle) {
    std::lock_guard<std::mutex> lock(endpoint_registry_mutex);
    auto it = endpoints.find(handle);
    return it == endpoints.end() ? nullptr : it->second;
}

void register_endpoint(const std::shared_ptr<Endpoint>& endpoint) {
    std::lock_guard<std::mutex> lock(endpoint_registry_mutex);
    endpoints[endpoint.get()] = endpoint;
}

bool start_winsock() {
    std::call_once(winsock_once, [] {
        WSADATA data;
        if (WSAStartup(MAKEWORD(2, 2), &data) != 0) throw std::runtime_error("WSAStartup failed");
    });
    return true;
}
}

extern "C" __declspec(dllexport) unsigned nc_abi() { return 1; }

extern "C" __declspec(dllexport) void* nc_channel_new() {
    try { return new Channel(); } catch (...) { return nullptr; }
}

extern "C" __declspec(dllexport) void nc_channel_free(void* handle) {
    delete static_cast<Channel*>(handle);
}

extern "C" __declspec(dllexport) const char* nc_channel_call(void* handle, int op, const char* input) {
    thread_local std::string result;
    try {
        if (!handle || !input) throw std::runtime_error("invalid channel handle or input");
        auto* channel = static_cast<Channel*>(handle);
        json in = json::parse(input), out;
        std::lock_guard<std::mutex> lock(channel->mutex);
        if (op == 1) {
            if (!in.is_array()) throw std::runtime_error("messages must be a list");
            if (channel->pending.size() + in.size() > WINDOW)
                throw std::runtime_error("reliable message queue is full");
            for (const auto& item : in) validate_compact(item);
            for (const auto& item : in) channel->pending.emplace(channel->next_message++, item);
            out = true;
        } else if (op == 2) {
            out = channel->make_packet(in);
        } else if (op == 3) {
            out = channel->receive(in);
        } else if (op == 4) {
            out = {{"pending", channel->pending.size()},
                   {"newest", channel->has_newest ? json(channel->newest) : json(nullptr)},
                   {"seq", channel->sequence}};
        } else {
            throw std::runtime_error("unknown operation");
        }
        result = json({{"ok", true}, {"value", out}}).dump();
    } catch (const std::exception& error) {
        result = json({{"ok", false}, {"error", error.what()}}).dump();
    } catch (...) {
        result = "{\"ok\":false,\"error\":\"native exception\"}";
    }
    return result.c_str();
}

extern "C" __declspec(dllexport) const char* nc_channel_wire(void* handle, const char* input, const char* token) {
    if (!handle || !input || !token) return nullptr;
    auto* channel = static_cast<Channel*>(handle);
    try {
        json in = json::parse(input);
        if (!in.is_object() || !in.contains("frame") || !in.contains("messages") || !in["messages"].is_array()) return nullptr;
        std::lock_guard<std::mutex> lock(channel->mutex);
        const json& frame = in["frame"];
        if (!frame.is_null() && !frame.is_object()) return nullptr;
        if (!frame.is_null()) validate_compact(frame);
        json base = {{"token", token}, {"s", channel->sequence},
                     {"a", channel->has_ack ? json(channel->ack) : json(nullptr)},
                     {"b", channel->ack_bits}, {"f", frame}, {"e", json::array()}};
        validate_compact(base);
        auto& messages = in["messages"];
        if (channel->pending.size() + messages.size() > WINDOW) return nullptr;
        for (const auto& message : messages) validate_compact(message);
        for (const auto& message : messages) channel->pending.emplace(channel->next_message++, message);
        json packet = channel->make_packet({{"frame", frame}, {"token", token}});
        channel->raw_result = packet.dump(-1, ' ', false, json::error_handler_t::strict);
        return channel->raw_result.c_str();
    } catch (...) { return nullptr; }
}

extern "C" __declspec(dllexport) const char* nc_channel_feed(void* handle, const char* raw, const char* token) {
    if (!handle || !raw || !token) return nullptr;
    auto* channel = static_cast<Channel*>(handle);
    try {
        size_t size = std::strlen(raw);
        if (size > MAX_DATAGRAM) return nullptr;
        json packet = json::parse(raw, raw + size);
        if (!packet.is_object() || !packet.contains("token") || !packet["token"].is_string() ||
            packet["token"].get<std::string>() != token) return nullptr;
        std::lock_guard<std::mutex> lock(channel->mutex);
        if (packet.value("hello", false) == true) {
            channel->raw_result = "{\"hello\":true}";
            return channel->raw_result.c_str();
        }
        json result = channel->receive(packet);
        result["data"] = !packet["f"].is_null() || !packet["e"].empty();
        channel->raw_result = result.dump(-1, ' ', false, json::error_handler_t::strict);
        return channel->raw_result.c_str();
    } catch (...) { return nullptr; }
}

extern "C" __declspec(dllexport) void* nc_udp_open(const char* ip, unsigned short port) {
    try {
        start_winsock();
        SOCKET socket_handle = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (socket_handle == INVALID_SOCKET) return nullptr;
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(port);
        if (inet_pton(AF_INET, ip, &address.sin_addr) != 1 ||
            connect(socket_handle, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == SOCKET_ERROR) {
            closesocket(socket_handle);
            return nullptr;
        }
        auto endpoint = std::make_shared<Endpoint>();
        endpoint->socket = socket_handle;
        endpoint->start();
        register_endpoint(endpoint);
        return endpoint.get();
    } catch (...) { return nullptr; }
}

extern "C" __declspec(dllexport) void* nc_udp_bind(const char* ip, unsigned short port) {
    try {
        start_winsock();
        SOCKET socket_handle = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (socket_handle == INVALID_SOCKET) return nullptr;
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_port = htons(port);
        if (!ip || inet_pton(AF_INET, ip, &address.sin_addr) != 1 ||
            bind(socket_handle, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == SOCKET_ERROR) {
            closesocket(socket_handle);
            return nullptr;
        }
        auto endpoint = std::make_shared<Endpoint>();
        endpoint->socket = socket_handle;
        endpoint->bound = true;
        endpoint->start();
        register_endpoint(endpoint);
        return endpoint.get();
    } catch (...) { return nullptr; }
}

extern "C" __declspec(dllexport) unsigned short nc_udp_port(void* handle) {
    auto endpoint = get_endpoint(handle);
    if (!endpoint || !endpoint->bound) return 0;
    sockaddr_in address{};
    int size = sizeof(address);
    if (getsockname(endpoint->socket, reinterpret_cast<sockaddr*>(&address), &size) == SOCKET_ERROR) return 0;
    return ntohs(address.sin_port);
}

extern "C" __declspec(dllexport) int nc_udp_send(void* handle, const char* data, int size) {
    auto endpoint = get_endpoint(handle);
    if (!endpoint || endpoint->bound || !data || size < 0 || size > static_cast<int>(MAX_DATAGRAM)) return -1;
    std::lock_guard<std::mutex> lock(endpoint->mutex);
    if (endpoint->stopped) return -1;
    int sent = send(endpoint->socket, data, size, 0);
    return sent == SOCKET_ERROR ? -1 : sent;
}

extern "C" __declspec(dllexport) int nc_udp_send_to(void* handle, const char* data, int size,
                                                     const char* ip, unsigned short port) {
    auto endpoint = get_endpoint(handle);
    if (!endpoint || !endpoint->bound || !data || size < 0 || size > static_cast<int>(MAX_DATAGRAM) || !ip) return -1;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_port = htons(port);
    if (inet_pton(AF_INET, ip, &address.sin_addr) != 1) return -1;
    std::lock_guard<std::mutex> lock(endpoint->mutex);
    if (endpoint->stopped) return -1;
    int sent = sendto(endpoint->socket, data, size, 0, reinterpret_cast<sockaddr*>(&address), sizeof(address));
    return sent == SOCKET_ERROR ? -1 : sent;
}

extern "C" __declspec(dllexport) int nc_udp_recv(void* handle, char* buffer, int capacity, int timeout) {
    auto endpoint = get_endpoint(handle);
    if (!endpoint || endpoint->bound || !buffer || capacity < 0) return -1;
    std::unique_lock<std::mutex> lock(endpoint->mutex);
    endpoint->ready.wait_for(lock, std::chrono::milliseconds(std::max(0, timeout)),
                             [&] { return endpoint->stopped || !endpoint->queue.empty(); });
    if (endpoint->queue.empty()) return endpoint->stopped ? -1 : 0;
    auto data = std::move(endpoint->queue.front().data);
    endpoint->queue.pop_front();
    if (data.size() > static_cast<size_t>(capacity)) return -1;
    std::memcpy(buffer, data.data(), data.size());
    return static_cast<int>(data.size());
}

extern "C" __declspec(dllexport) int nc_udp_recv_from(void* handle, char* buffer, int capacity,
                                                       int timeout, char* ip, unsigned short* port) {
    auto endpoint = get_endpoint(handle);
    if (!endpoint || !endpoint->bound || !buffer || capacity < 0 || !ip || !port) return -1;
    std::unique_lock<std::mutex> lock(endpoint->mutex);
    endpoint->ready.wait_for(lock, std::chrono::milliseconds(std::max(0, timeout)),
                             [&] { return endpoint->stopped || !endpoint->queue.empty(); });
    if (endpoint->queue.empty()) return endpoint->stopped ? -1 : 0;
    auto datagram = std::move(endpoint->queue.front());
    endpoint->queue.pop_front();
    if (datagram.data.size() > static_cast<size_t>(capacity)) return -1;
    std::memcpy(buffer, datagram.data.data(), datagram.data.size());
    if (!InetNtopA(AF_INET, &datagram.peer.sin_addr, ip, 16)) return -1;
    *port = ntohs(datagram.peer.sin_port);
    return static_cast<int>(datagram.data.size());
}

extern "C" __declspec(dllexport) void nc_udp_close(void* handle) {
    std::shared_ptr<Endpoint> endpoint;
    {
        std::lock_guard<std::mutex> lock(endpoint_registry_mutex);
        auto it = endpoints.find(handle);
        if (it == endpoints.end()) return;
        endpoint = it->second;
        endpoints.erase(it);
    }
    endpoint->stop();
}
