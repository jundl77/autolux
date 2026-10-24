#pragma once

#include "radius.pb.h"
#include <array>
#include <cstdint>
#include <optional>
#include <core/njson.h>
#include <core/types.h>

namespace Radius::Bus {

constexpr size_t AudioFrames = 128;
constexpr size_t Channels = 2;
constexpr uint32_t SampleRateHz = 44100;
constexpr size_t AudioBytesPerFrame = Channels * sizeof(float);
constexpr size_t MaxDatagramBytes = 1200; // Serialized payload budget, excluding IP/UDP headers.
constexpr uint32_t ProtocolVersion = 2;
constexpr uint64_t HeartbeatMilliseconds = 10000;
constexpr uint64_t TimeoutMilliseconds = 30000;
constexpr uint64_t HealthReportIntervalMilliseconds = 1000;

// Fixed storage crosses the audio callback boundary; protobuf stays on the network thread.
struct AudioBlock
{
	uint64_t watermark = 0;
	uint16_t frames = 0;
	std::array<float, AudioFrames * Channels> samples{};
};

struct LightingFrame
{
	uint64_t watermark = 0;
	std::array<uint8_t, DmxFrameSize> channels{};
};

Envelope NewEnvelope(uint64_t session);
bool IsValidEnvelope(const Envelope& packet, uint64_t expectedSession = 0);
bool IsNewerSequenceNumber(uint32_t incoming, std::optional<uint32_t> previous);
size_t SerializeEnvelope(const Envelope& packet, std::array<uint8_t, MaxDatagramBytes>& bytes);
bool ParseEnvelope(const void* data, size_t size, uint64_t expectedSession, Envelope& packet);
njson ReceiverTelemetryToJson(const ReceiverTelemetry& health);

} // namespace Radius::Bus
