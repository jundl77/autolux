#pragma once

#include "radius.pb.h"
#include <array>
#include <cstdint>
#include <optional>
#include <variant>
#include <core/njson.h>
#include <core/types.h>

namespace Radius::Bus {

constexpr size_t AudioFrames = 128;
constexpr size_t Channels = 2;
constexpr uint32_t SampleRateHz = 44100;
constexpr size_t AudioBytesPerFrame = Channels * sizeof(float);
constexpr size_t MaxDatagramBytes = 1200; // Serialized payload budget, excluding IP/UDP headers.
constexpr size_t FrameHeaderBytes = 5;
constexpr size_t MaxErrorBytes = 512;
constexpr uint64_t LoginRetryMilliseconds = 250;
constexpr uint64_t LoginTimeoutMilliseconds = 15000;
constexpr uint64_t LoginCommandId = 1;
constexpr uint32_t MinJitterBufferMilliseconds = 20, MaxJitterBufferMilliseconds = 150;
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

using Message = std::variant<Login, LoginReply, ErrorReply, AudioFrame, LightFrame, Heartbeat, Metrics>;

MessageType TypeOf(const Message& message);
bool IsValidMessage(const Message& message, uint64_t expectedRunId = 0);
bool IsNewerSequenceNumber(uint32_t incoming, std::optional<uint32_t> previous);
size_t SerializeMessage(const Message& message, std::array<uint8_t, MaxDatagramBytes>& bytes);
bool ParseMessage(const void* data, size_t size, uint64_t expectedRunId, Message& message);
njson MetricsToJson(const Metrics& metrics);

} // namespace Radius::Bus
