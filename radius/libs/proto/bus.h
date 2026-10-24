#pragma once
#include <array>
#include <cstdint>
#include <cstring>

namespace Radius::Bus {

constexpr size_t AudioFrames = 128;
constexpr size_t Channels = 2;
constexpr size_t MaxPayload = AudioFrames * Channels * sizeof(float);
constexpr size_t HeaderSize = 32;
constexpr size_t MaxDatagram = HeaderSize + MaxPayload;
constexpr uint64_t HeartbeatMilliseconds = 10000;
constexpr uint64_t TimeoutMilliseconds = 30000;
enum class Kind : uint8_t
{
	Audio = 1,
	Dmx = 2,
	Heartbeat = 3,
	Health = 4
};

struct AudioBlock
{
	uint64_t watermark = 0; // First stereo sample frame on this session's media clock.
	uint16_t frames = 0;
	std::array<float, AudioFrames * Channels> samples{};
};

struct Packet
{
	Kind kind = Kind::Heartbeat;
	uint64_t session = 0;
	uint64_t watermark = 0;
	uint32_t sequence = 0;
	uint16_t size = 0;
	std::array<uint8_t, MaxPayload> payload{};
};

// Explicit little endian wire encoding, independent of padding/alignment.
inline void Put(uint8_t* p, uint64_t value, size_t bytes)
{
	for (size_t i = 0; i < bytes; ++i)
		p[i] = static_cast<uint8_t>(value >> (8 * i));
}

inline uint64_t Get(const uint8_t* p, size_t bytes)
{
	uint64_t value = 0;
	for (size_t i = 0; i < bytes; ++i)
		value |= uint64_t(p[i]) << (8 * i);
	return value;
}

inline size_t Encode(const Packet& p, std::array<uint8_t, MaxDatagram>& bytes)
{
	if (p.size > MaxPayload)
		return 0;
	bytes.fill(0);
	Put(bytes.data(), 0x53554452, 4); // RDUS
	bytes[4] = 1;
	bytes[5] = static_cast<uint8_t>(p.kind);
	Put(bytes.data() + 6, p.size, 2);
	Put(bytes.data() + 8, p.session, 8);
	Put(bytes.data() + 16, p.watermark, 8);
	Put(bytes.data() + 24, p.sequence, 4);
	std::memcpy(bytes.data() + HeaderSize, p.payload.data(), p.size);
	return HeaderSize + p.size;
}

inline bool Decode(const void* raw, size_t size, Packet& p)
{
	if (size < HeaderSize || size > MaxDatagram)
		return false;
	const auto* b = static_cast<const uint8_t*>(raw);
	if (Get(b, 4) != 0x53554452 || b[4] != 1 || Get(b + 28, 4) != 0)
		return false;
	const auto length = Get(b + 6, 2);
	if (size != HeaderSize + length || !Get(b + 8, 8))
		return false;
	const auto kind = static_cast<Kind>(b[5]);
	if (kind == Kind::Audio)
	{
		if (!length || length % 8 || Get(b + 16, 8) > UINT64_MAX - length / 8)
			return false;
	}
	else if (kind == Kind::Dmx)
	{
		if (length != 512)
			return false;
	}
	else if (kind == Kind::Heartbeat)
	{
		if (length != 0)
			return false;
	}
	else if (kind == Kind::Health)
	{
		if (!length)
			return false;
	}
	else
		return false;
	p.kind = kind;
	p.size = static_cast<uint16_t>(length);
	p.session = Get(b + 8, 8);
	p.watermark = Get(b + 16, 8);
	p.sequence = static_cast<uint32_t>(Get(b + 24, 4));
	std::memcpy(p.payload.data(), b + HeaderSize, static_cast<size_t>(length));
	return true;
}

} // namespace Radius::Bus
