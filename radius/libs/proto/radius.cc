#include "radius.h"
#include <cmath>

namespace Radius::Bus {

Envelope NewEnvelope(uint64_t session)
{
	Envelope packet;
	packet.set_protocol_version(ProtocolVersion);
	packet.set_session_id(session);
	return packet;
}

bool IsNewerSequenceNumber(uint32_t incoming, std::optional<uint32_t> previous)
{
	// Modulo-2^32 ordering accepts forward distances below half the sequence range.
	return !previous || static_cast<int32_t>(incoming - *previous) > 0;
}

bool IsValidEnvelope(const Envelope& packet, uint64_t expectedSession)
{
	if (packet.protocol_version() != ProtocolVersion || !packet.session_id() ||
	    (expectedSession && packet.session_id() != expectedSession))
		return false;
	switch (packet.payload_case())
	{
	case Envelope::kAudio: {
		const auto& audio = packet.audio();
		const auto samples = audio.interleaved_samples_size();
		if (audio.sample_rate_hz() != SampleRateHz || audio.channel_count() != Channels || samples <= 0 ||
		    samples > AudioFrames * Channels || samples % Channels ||
		    packet.audio_frame_position() > UINT64_MAX - samples / Channels)
			return false;
		for (float sample : audio.interleaved_samples())
			if (!std::isfinite(sample))
				return false;
		return true;
	}
	case Envelope::kLighting:
		return packet.lighting().channels().size() == DmxFrameSize;
	case Envelope::kHeartbeat:
		return packet.audio_frame_position() == 0 && packet.sequence_number() == 0;
	case Envelope::kReceiverTelemetry: {
		const auto& health = packet.receiver_telemetry();
		const auto nonnegative = [](double value) {
			return std::isfinite(value) && value >= 0;
		};
		return packet.audio_frame_position() == 0 && packet.sequence_number() == 0 && health.up() &&
		       nonnegative(health.heartbeat_age_seconds()) && nonnegative(health.audio_buffer_seconds()) &&
		       std::isfinite(health.buffer_target_seconds()) && health.buffer_target_seconds() > 0 &&
		       nonnegative(health.dmx_lateness_seconds()) && nonnegative(health.audio_signal_peak()) &&
		       health.has_audio_input_age_seconds() == health.has_audio_max_gap_seconds() &&
		       (!health.has_audio_input_age_seconds() ||
		        (nonnegative(health.audio_input_age_seconds()) && nonnegative(health.audio_max_gap_seconds())));
	}
	default:
		return false;
	}
}

size_t SerializeEnvelope(const Envelope& packet, std::array<uint8_t, MaxDatagramBytes>& bytes)
{
	if (!IsValidEnvelope(packet))
		return 0;
	const auto size = packet.ByteSizeLong();
	if (size > bytes.size() || !packet.SerializeToArray(bytes.data(), static_cast<int>(size)))
		return 0;
	return size;
}

bool ParseEnvelope(const void* data, size_t size, uint64_t expectedSession, Envelope& packet)
{
	if (!data || !size || size > MaxDatagramBytes)
		return false;
	return packet.ParseFromArray(data, static_cast<int>(size)) && IsValidEnvelope(packet, expectedSession);
}

njson ReceiverTelemetryToJson(const ReceiverTelemetry& health)
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
