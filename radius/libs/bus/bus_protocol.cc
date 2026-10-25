#include <bus/bus_protocol.h>
#include <cmath>
#include <cstring>
#include <type_traits>

namespace Radius::Bus {
namespace {

bool ValidPayload(const Login& value)
{
	return value.command_id() != 0;
}

bool ValidPayload(const LoginReply& value)
{
	return value.command_id() != 0;
}

bool ValidPayload(const ErrorReply& value)
{
	return value.command_id() && !value.message().empty() && value.message().size() <= MaxErrorBytes;
}

bool ValidPayload(const Heartbeat&)
{
	return true;
}

bool ValidPayload(const LightFrame& value)
{
	return value.channels().size() == DmxFrameSize;
}

bool ValidPayload(const AudioFrame& value)
{
	const auto samples = value.interleaved_samples_size();
	if (samples <= 0 || samples > AudioFrames * Channels || samples % Channels ||
	    value.audio_frame_position() > UINT64_MAX - samples / Channels)
		return false;
	for (float sample : value.interleaved_samples())
		if (!std::isfinite(sample))
			return false;
	return true;
}

bool ValidPayload(const Metrics& health)
{
	const auto nonnegative = [](double value) {
		return std::isfinite(value) && value >= 0;
	};
	return health.up() && health.last_processed_command_id() && nonnegative(health.heartbeat_age_seconds()) &&
	       nonnegative(health.audio_buffer_seconds()) && std::isfinite(health.buffer_target_seconds()) &&
	       health.buffer_target_seconds() > 0 && nonnegative(health.dmx_lateness_seconds()) &&
	       nonnegative(health.audio_signal_peak()) &&
	       health.has_audio_input_age_seconds() == health.has_audio_max_gap_seconds() &&
	       (!health.has_audio_input_age_seconds() ||
	        (nonnegative(health.audio_input_age_seconds()) && nonnegative(health.audio_max_gap_seconds())));
}

template <class T> bool Decode(const uint8_t* data, size_t size, Message& message)
{
	T value;
	if (!value.ParseFromArray(data, static_cast<int>(size)))
		return false;
	message = std::move(value);
	return true;
}

} // namespace

MessageType TypeOf(const Message& message)
{
	return std::visit(
	    [](const auto& value) {
		    using T = std::decay_t<decltype(value)>;
		    if constexpr (std::is_same_v<T, Login>)
			    return LOGIN;
		    else if constexpr (std::is_same_v<T, LoginReply>)
			    return LOGIN_REPLY;
		    else if constexpr (std::is_same_v<T, ErrorReply>)
			    return ERROR_REPLY;
		    else if constexpr (std::is_same_v<T, AudioFrame>)
			    return AUDIO_FRAME;
		    else if constexpr (std::is_same_v<T, LightFrame>)
			    return LIGHT_FRAME;
		    else if constexpr (std::is_same_v<T, Heartbeat>)
			    return HEARTBEAT;
		    else
			    return METRICS;
	    },
	    message);
}

bool IsNewerSequenceNumber(uint32_t incoming, std::optional<uint32_t> previous)
{
	// Modulo-2^32 ordering accepts forward distances below half the sequence range.
	return !previous || static_cast<int32_t>(incoming - *previous) > 0;
}

bool IsValidMessage(const Message& message, uint64_t expectedRunId)
{
	return std::visit(
	    [&](const auto& value) {
		    return value.run_id() && (!expectedRunId || value.run_id() == expectedRunId) && ValidPayload(value);
	    },
	    message);
}

size_t SerializeMessage(const Message& message, std::array<uint8_t, MaxDatagramBytes>& bytes)
{
	if (!IsValidMessage(message))
		return 0;
	return std::visit(
	    [&](const auto& value) -> size_t {
		    const auto size = value.ByteSizeLong();
		    if (size > bytes.size() - FrameHeaderBytes ||
			    !value.SerializeToArray(bytes.data() + FrameHeaderBytes, static_cast<int>(size)))
			    return 0;
		    bytes[0] = static_cast<uint8_t>(TypeOf(message));
		    for (size_t i = 1; i < FrameHeaderBytes; ++i)
			    bytes[i] = static_cast<uint8_t>(size >> (8 * (FrameHeaderBytes - i - 1)));
		    return FrameHeaderBytes + size;
	    },
	    message);
}

bool ParseMessage(const void* data, size_t size, uint64_t expectedRunId, Message& message)
{
	if (!data || size < FrameHeaderBytes || size > MaxDatagramBytes)
		return false;
	const auto* bytes = static_cast<const uint8_t*>(data);
	uint32_t length = 0;
	for (size_t i = 1; i < FrameHeaderBytes; ++i)
		length = (length << 8) | bytes[i];
	if (length != size - FrameHeaderBytes)
		return false;
	const auto* payload = bytes + FrameHeaderBytes;
	bool parsed = false;
	switch (bytes[0])
	{
	case LOGIN:
		parsed = Decode<Login>(payload, length, message);
		break;
	case LOGIN_REPLY:
		parsed = Decode<LoginReply>(payload, length, message);
		break;
	case ERROR_REPLY:
		parsed = Decode<ErrorReply>(payload, length, message);
		break;
	case AUDIO_FRAME:
		parsed = Decode<AudioFrame>(payload, length, message);
		break;
	case LIGHT_FRAME:
		parsed = Decode<LightFrame>(payload, length, message);
		break;
	case HEARTBEAT:
		parsed = Decode<Heartbeat>(payload, length, message);
		break;
	case METRICS:
		parsed = Decode<Metrics>(payload, length, message);
		break;
	default:
		return false;
	}
	return parsed && IsValidMessage(message, expectedRunId);
}

njson MetricsToJson(const Metrics& health)
{
	return {{"up", health.up()},
	        {"link_up", health.link_up()},
	        {"heartbeat_age_seconds", health.heartbeat_age_seconds()},
	        {"heartbeats_sent", health.heartbeats_sent()},
	        {"heartbeats_received", health.heartbeats_received()},
	        {"audio_buffer_seconds", health.audio_buffer_seconds()},
	        {"buffer_target_seconds", health.buffer_target_seconds()},
	        {"underruns_total", health.underruns_total()},
	        {"device_underruns_total", health.device_underruns_total()},
	        {"concealed_frames_total", health.concealed_frames_total()},
	        {"lost_frames_total", health.lost_frames_total()},
	        {"overflow_frames_total", health.overflow_frames_total()},
	        {"late_packets_total", health.late_packets_total()},
	        {"latency_trims_total", health.latency_trims_total()},
	        {"clock_correction_ppm", health.clock_correction_ppm()},
	        {"dmx_connected", health.dmx_connected()},
	        {"dmx_frames_total", health.dmx_frames_total()},
	        {"dmx_dropped_total", health.dmx_dropped_total()},
	        {"dmx_lateness_seconds", health.dmx_lateness_seconds()},
	        {"audio_input_age_seconds",
	         health.has_audio_input_age_seconds() ? njson(health.audio_input_age_seconds()) : njson(nullptr)},
	        {"audio_max_gap_seconds",
	         health.has_audio_max_gap_seconds() ? njson(health.audio_max_gap_seconds()) : njson(nullptr)},
	        {"audio_signal_peak", health.audio_signal_peak()},
	        {"invalid_packets_total", health.invalid_packets_total()},
	        {"send_failures_total", health.send_failures_total()}};
}

} // namespace Radius::Bus
