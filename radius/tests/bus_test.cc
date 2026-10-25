#include <bus/bus_protocol.h>
#include <bus/artdmx.h>
#include <network/link_state.h>
#include <soundio/playout.h>
#include <core/spsc_queue.h>
#include <thread>
#include <iostream>
#include <stdexcept>
#include <cmath>
#include <limits>
using namespace Radius;

void Check(bool ok, const char* message)
{
	if (!ok)
		throw std::runtime_error(message);
}

Bus::AudioBlock Block(uint64_t start)
{
	Bus::AudioBlock block;
	block.watermark = start;
	block.frames = 128;
	for (size_t i = 0; i < 128; ++i)
		block.samples[2 * i] = block.samples[2 * i + 1] = float((start + i) % 100) / 100;
	return block;
}

void CheckProtocol()
{
	constexpr uint32_t HalfSequenceRange = uint32_t(INT32_MAX) + 1;
	Check(Bus::IsNewerSequenceNumber(0, std::nullopt), "initial zero sequence number accepted");
	Check(Bus::IsNewerSequenceNumber(UINT32_MAX, std::nullopt), "initial maximum sequence number accepted");
	Check(Bus::IsNewerSequenceNumber(0, UINT32_MAX), "sequence number wraps forward to zero");
	Check(!Bus::IsNewerSequenceNumber(0, 0), "duplicate zero sequence number rejected");
	Check(!Bus::IsNewerSequenceNumber(UINT32_MAX, 0), "late pre-wrap sequence number rejected");
	Check(!Bus::IsNewerSequenceNumber(HalfSequenceRange, 0), "half-range sequence jump rejected");
	Check(Bus::IsNewerSequenceNumber(HalfSequenceRange - 1, 0), "forward sequence number within half range");
	constexpr uint64_t ReceivedAtMilliseconds = 100, InputTimeoutMilliseconds = 1000;
	Check(IsInputStale(0, 0, InputTimeoutMilliseconds), "missing input is timed out");
	Check(!IsInputStale(ReceivedAtMilliseconds + InputTimeoutMilliseconds, ReceivedAtMilliseconds,
	                    InputTimeoutMilliseconds),
	      "input timeout boundary remains fresh");
	Check(IsInputStale(ReceivedAtMilliseconds + InputTimeoutMilliseconds + 1, ReceivedAtMilliseconds,
	                   InputTimeoutMilliseconds),
	      "input beyond timeout is stale");
	Check(!IsInputStale(ReceivedAtMilliseconds - 1, ReceivedAtMilliseconds, InputTimeoutMilliseconds),
	      "concurrent future input timestamp must not underflow into a timeout");
	Check(!IsInputStale(0, UINT64_MAX, InputTimeoutMilliseconds), "full-range future input remains fresh");
	constexpr uint64_t Session = UINT64_MAX;
	Bus::AudioFrame audio;
	audio.set_session_id(Session);
	audio.set_audio_frame_position(UINT64_MAX - Bus::AudioFrames);
	audio.set_sequence_number(UINT32_MAX);
	const std::array<float, 4> extremes = {std::numeric_limits<float>::max(), std::numeric_limits<float>::lowest(),
	                                       std::numeric_limits<float>::denorm_min(), -0.0f};
	for (size_t i = 0; i < Bus::AudioFrames * Bus::Channels; ++i)
		audio.add_interleaved_samples(extremes[i % extremes.size()]);
	std::array<uint8_t, Bus::MaxDatagramBytes> wire{};
	const auto size = Bus::SerializeMessage(audio, wire);
	Check(size > Bus::FrameHeaderBytes && size <= Bus::MaxDatagramBytes, "audio MTU budget");
	Check(wire[0] == Bus::AUDIO_FRAME && wire[1] == 0 && wire[2] == 0, "standalone generated type and length header");
	Check((uint32_t(wire[3]) << 8 | wire[4]) == size - Bus::FrameHeaderBytes, "big endian payload length");
	Bus::Message decoded;
	Check(Bus::ParseMessage(wire.data(), size, Session, decoded), "protobuf audio round trip");
	const auto& decodedAudio = std::get<Bus::AudioFrame>(decoded);
	Check(decodedAudio.session_id() == Session && decodedAudio.audio_frame_position() == audio.audio_frame_position() &&
	          decodedAudio.sequence_number() == UINT32_MAX,
	      "exact 64-bit identifiers and sequence");
	Check(decodedAudio.interleaved_samples_size() == Bus::AudioFrames * Bus::Channels, "audio sample count");
	Check(std::memcmp(decodedAudio.interleaved_samples().data(), audio.interleaved_samples().data(),
	                  Bus::AudioFrames * Bus::AudioBytesPerFrame) == 0,
	      "IEEE float samples preserve bits");
	for (size_t n = 0; n < size; ++n)
		Check(!Bus::ParseMessage(wire.data(), n, Session, decoded), "truncated frame accepted");
	Check(!Bus::ParseMessage(wire.data(), size, Session - 1, decoded), "wrong session accepted");
	std::array<uint8_t, Bus::MaxDatagramBytes + 1> oversized{};
	Check(!Bus::ParseMessage(oversized.data(), oversized.size(), Session, decoded), "oversize datagram accepted");
	auto invalidWire = wire;
	invalidWire[0] = 255;
	Check(!Bus::ParseMessage(invalidWire.data(), size, Session, decoded), "unknown message type accepted");
	invalidWire = wire;
	++invalidWire[4];
	Check(!Bus::ParseMessage(invalidWire.data(), size, Session, decoded), "wrong declared length accepted");
	invalidWire = wire;
	invalidWire[0] = Bus::HEARTBEAT;
	Check(Bus::ParseMessage(invalidWire.data(), size, Session, decoded),
	      "unknown protobuf fields remain minor-compatible");

	const auto rejects = [&](const Bus::Message& invalid) {
		Check(Bus::SerializeMessage(invalid, wire) == 0, "invalid outgoing message accepted");
		// Frame the generated payload directly to exercise incoming semantic validation.
		const auto raw = std::visit(
		    [](const auto& message) {
			    return message.SerializeAsString();
		    },
		    invalid);
		std::string framed(Bus::FrameHeaderBytes, '\0');
		framed[0] = static_cast<char>(Bus::TypeOf(invalid));
		for (size_t i = 1; i < Bus::FrameHeaderBytes; ++i)
			framed[i] = static_cast<char>(raw.size() >> (8 * (Bus::FrameHeaderBytes - i - 1)));
		framed += raw;
		Check(!Bus::ParseMessage(framed.data(), framed.size(), Session, decoded), "invalid incoming message accepted");
	};
	audio.set_session_id(0);
	rejects(audio);
	audio.set_session_id(Session);
	audio.set_audio_frame_position(UINT64_MAX - Bus::AudioFrames + 1);
	rejects(audio);
	audio.set_audio_frame_position(0);
	audio.set_interleaved_samples(0, std::numeric_limits<float>::quiet_NaN());
	rejects(audio);
	audio.set_interleaved_samples(0, std::numeric_limits<float>::infinity());
	rejects(audio);
	audio.set_interleaved_samples(0, 0);
	audio.add_interleaved_samples(0);
	rejects(audio);
	audio.mutable_interleaved_samples()->Clear();
	rejects(audio);
	audio.add_interleaved_samples(0);
	rejects(audio);
	Bus::Heartbeat heartbeat;
	heartbeat.set_session_id(Session);
	Check(Bus::SerializeMessage(heartbeat, wire) > 0, "heartbeat session without command ID");
	Bus::LightFrame lighting;
	lighting.set_session_id(Session);
	lighting.set_audio_frame_position(UINT64_MAX);
	std::string dmx(DmxFrameSize, '\0');
	for (size_t channel = 0; channel < dmx.size(); ++channel)
		dmx[channel] = static_cast<char>(channel);
	lighting.set_channels(dmx);
	const auto lightingSize = Bus::SerializeMessage(lighting, wire);
	Check(lightingSize && Bus::ParseMessage(wire.data(), lightingSize, Session, decoded) &&
	          std::get<Bus::LightFrame>(decoded).audio_frame_position() == UINT64_MAX &&
	          std::get<Bus::LightFrame>(decoded).channels() == dmx,
	      "lighting bytes and maximum watermark");
	lighting.set_channels(dmx.substr(1));
	rejects(lighting);
	lighting.set_channels(dmx + '\0');
	rejects(lighting);

	Bus::Login login;
	login.set_session_id(Session);
	rejects(login);
	login.set_command_id(Bus::LoginCommandId);
	login.set_protocol_major(Bus::PROTOCOL_MAJOR);
	login.set_protocol_minor(Bus::PROTOCOL_MINOR + 1);
	login.set_sample_rate_hz(Bus::SampleRateHz);
	login.set_channel_count(Bus::Channels);
	login.set_audio_enabled(true);
	login.set_lighting_enabled(true);
	login.set_jitter_buffer_ms(150);
	const auto loginSize = Bus::SerializeMessage(login, wire);
	Check(loginSize && Bus::ParseMessage(wire.data(), loginSize, Session, decoded) &&
	          std::get<Bus::Login>(decoded).protocol_minor() == Bus::PROTOCOL_MINOR + 1,
	      "login preserves initialization and negotiable version");
	Bus::ErrorReply error;
	error.set_session_id(Session);
	error.set_command_id(Bus::LoginCommandId);
	rejects(error);
	error.set_message(std::string(Bus::MaxErrorBytes + 1, 'x'));
	rejects(error);

	Bus::Metrics health;
	health.set_session_id(Session);
	health.set_last_processed_command_id(Bus::LoginCommandId);
	health.set_up(true);
	health.set_link_up(true);
	health.set_heartbeat_age_seconds(0.5);
	health.set_audio_buffer_seconds(0.125);
	health.set_buffer_target_seconds(0.15);
	health.set_clock_correction_ppm(-999);
	health.set_dmx_lateness_seconds(0.025);
	health.set_audio_signal_peak(0.75f);
	health.set_heartbeats_sent(UINT64_MAX);
	health.set_heartbeats_received(UINT64_MAX);
	health.set_underruns_total(UINT64_MAX);
	health.set_device_underruns_total(UINT64_MAX);
	health.set_concealed_frames_total(UINT64_MAX);
	health.set_lost_frames_total(UINT64_MAX);
	health.set_overflow_frames_total(UINT64_MAX);
	health.set_late_packets_total(UINT64_MAX);
	health.set_latency_trims_total(UINT64_MAX);
	health.set_dmx_frames_total(UINT64_MAX);
	health.set_dmx_dropped_total(UINT64_MAX);
	health.set_invalid_packets_total(UINT64_MAX);
	health.set_send_failures_total(UINT64_MAX);
	auto metricsSize = Bus::SerializeMessage(health, wire);
	Check(metricsSize && Bus::ParseMessage(wire.data(), metricsSize, Session, decoded), "metrics round trip");
	auto json = Bus::MetricsToJson(std::get<Bus::Metrics>(decoded));
	Check(json.size() == 24 && json["up"] == true && json["link_up"] == true && json["dmx_connected"] == false,
	      "dashboard health schema");
	Check(json["audio_input_age_seconds"].is_null() && json["audio_max_gap_seconds"].is_null(),
	      "missing input stays unavailable");
	for (const auto* key : {"heartbeats_sent", "heartbeats_received", "underruns_total", "device_underruns_total",
	                        "concealed_frames_total", "lost_frames_total", "overflow_frames_total",
	                        "late_packets_total", "latency_trims_total", "dmx_frames_total", "dmx_dropped_total",
	                        "invalid_packets_total", "send_failures_total"})
		Check(json[key].is_number_unsigned() && json[key].get<uint64_t>() == UINT64_MAX,
		      "metrics counters retain full uint64 range");
	Check(json["heartbeat_age_seconds"] == 0.5 && json["audio_buffer_seconds"] == 0.125 &&
	          json["buffer_target_seconds"] == 0.15 && json["clock_correction_ppm"] == -999 &&
	          json["dmx_lateness_seconds"] == 0.025 && json["audio_signal_peak"] == 0.75,
	      "metrics units unchanged");
	health.set_audio_input_age_seconds(0);
	health.set_audio_max_gap_seconds(0);
	metricsSize = Bus::SerializeMessage(health, wire);
	Check(metricsSize && Bus::ParseMessage(wire.data(), metricsSize, Session, decoded), "zero input age round trip");
	json = Bus::MetricsToJson(std::get<Bus::Metrics>(decoded));
	Check(json["audio_input_age_seconds"] == 0 && json["audio_max_gap_seconds"] == 0,
	      "measured zero remains distinct from unavailable");
	health.set_audio_input_age_seconds(-1);
	rejects(health);
	health.clear_audio_input_age_seconds();
	rejects(health);
	health.clear_audio_max_gap_seconds();
	health.set_audio_signal_peak(std::numeric_limits<float>::quiet_NaN());
	rejects(health);
	health.set_audio_signal_peak(0);
	health.set_buffer_target_seconds(0);
	rejects(health);
}

int main()
{
	CheckProtocol();
	std::array<uint8_t, 530> art{};
	std::memcpy(art.data(), "Art-Net\0", 8);
	art[9] = 0x50;
	art[11] = 14;
	art[14] = 1;
	art[16] = 2;
	art[18] = 255;
	std::array<uint8_t, 512> channels{};
	uint16_t universe = 0;
	Check(DecodeArtDmx(art.data(), art.size(), channels, universe) && universe == 1 && channels[0] == 255,
	      "Starfire ArtDmx");
	Check(!DecodeArtDmx(art.data(), 529, channels, universe), "truncated ArtDmx");
	art[16] = 3;
	Check(!DecodeArtDmx(art.data(), 530, channels, universe), "oversized ArtDmx");
	art[16] = 0;
	art[17] = 2;
	Check(DecodeArtDmx(art.data(), 20, channels, universe) && channels[2] == 0, "short ArtDmx clears stale tail");
	LinkState link(0);
	Check(link.Due(0) && !link.Up(0), "initial heartbeat");
	link.Sent(0);
	link.Received(1);
	Check(!link.Due(9999) && link.Due(10000), "10-second heartbeat");
	Check(link.Up(30000) && link.Expired(30001), "missing heartbeat timeout");
	SpscQueue<uint64_t, 64> queue;
	std::thread producer([&] {
		for (uint64_t i = 0; i < 1000000; ++i)
			while (!queue.Push(i))
				std::this_thread::yield();
	});
	for (uint64_t i = 0; i < 1000000; ++i)
	{
		const uint64_t* next;
		while (!(next = queue.Front()))
			std::this_thread::yield();
		Check(*next == i, "queue ownership/order");
		queue.Pop();
	}
	producer.join();
	Check(queue.Size() == 0, "queue drain");
	auto overflow = std::make_unique<Playout>(44100, 80);
	Check(!overflow->Push(Block(UINT64_MAX)), "playout watermark overflow");
	for (uint64_t i = 0; i < 256; ++i)
		Check(overflow->Push(Block(i * 128)), "queue capacity");
	Check(!overflow->Push(Block(256 * 128)) && overflow->overflowFrames == 128, "overflow bound");
	overflow->BeginCallback();
	float stereo[2];
	overflow->Frame(stereo);
	overflow->BeginCallback();
	Check(overflow->latencyTrims > 0, "latency recovery");
	Check(overflow->Push(Block(257 * 128)) && overflow->lostFrames == 0,
	      "receiver queue overflow must not count again as network loss");
	constexpr int PrimingBlocks = 8;
	auto boundary = std::make_unique<Playout>(Bus::SampleRateHz, 20);
	for (uint64_t i = 0; i < PrimingBlocks; ++i)
		Check(boundary->Push(Block(UINT64_MAX - (PrimingBlocks - i) * Bus::AudioFrames)),
		      "valid audio near maximum watermark");
	boundary->BeginCallback();
	for (size_t frame = 0; frame < Bus::AudioFrames; ++frame)
		boundary->Frame(stereo);
	boundary->BeginCallback();
	Check(boundary->latencyTrims == 0 && boundary->bufferedFrames > 0,
	      "watermark addition must not wrap into false latency recovery");
	auto gap = std::make_unique<Playout>(Bus::SampleRateHz, 20);
	Check(gap->Push(Block(0)) && gap->Push(Block(uint64_t(INT32_MAX) + Bus::AudioFrames)), "large gap accepted");
	gap->BeginCallback();
	gap->Frame(stereo);
	Check(gap->latencyTrims == 1 && gap->bufferedFrames == Bus::AudioFrames && gap->played == 0,
	      "large initial gap must rebuffer without narrowing overflow or fabricated buffered audio");
	auto extremes = std::make_unique<Playout>(Bus::SampleRateHz, 20);
	for (uint64_t i = 0; i < PrimingBlocks; ++i)
	{
		auto block = Block(i * Bus::AudioFrames);
		for (size_t frame = 0; frame < Bus::AudioFrames; ++frame)
			block.samples[frame * Bus::Channels] = block.samples[frame * Bus::Channels + 1] =
			    frame % 2 ? std::numeric_limits<float>::max() : std::numeric_limits<float>::lowest();
		Check(extremes->Push(block), "finite sample limits accepted");
	}
	extremes->BeginCallback();
	for (size_t frame = 0; frame < Bus::AudioFrames; ++frame)
	{
		extremes->Frame(stereo);
		Check(std::isfinite(stereo[0]) && std::isfinite(stereo[1]), "interpolation of finite samples stays finite");
	}
	auto loss = std::make_unique<Playout>(44100, 20);
	for (uint64_t i = 0; i < 20; ++i)
		if (i != 5)
			loss->Push(Block(i * 128));
	Check(!loss->Push(Block(0)) && loss->latePackets == 1, "late packet rejected");
	Check(loss->lostFrames == 128, "loss accounted");
	loss->BeginCallback();
	for (int i = 0; i < 4000; ++i)
		loss->Frame(stereo);
	Check(loss->concealedFrames >= 128 && loss->underruns == 1, "loss and starvation concealed");
	// Five simulated minutes with a capture clock 200 ppm faster than playback.
	auto stable = std::make_unique<Playout>(44100, 80);
	uint64_t produced = 0;
	double available = 4096;
	for (int callback = 0; callback < 30000; ++callback)
	{
		available += 441.0 * 1.0002;
		while (available >= 128)
		{
			Check(stable->Push(Block(produced)), "steady queue overflow");
			produced += 128;
			available -= 128;
		}
		stable->BeginCallback();
		for (int frame = 0; frame < 441; ++frame)
		{
			stable->Frame(stereo);
			Check(std::isfinite(stereo[0]), "finite samples");
		}
	}
	Check(stable->underruns == 0 && stable->overflowFrames == 0 && stable->latencyTrims == 0,
	      "clock drift causes stalls");
	Check(stable->bufferedFrames > 2000 && stable->bufferedFrames < 6000, "bounded drift buffer");
	// A Wi-Fi bridge can hold packets for 120 ms and then deliver the complete burst.
	// This is jitter, not loss: the default buffer must play it without starvation.
	const auto burstUnderruns = [](int targetMs) {
		auto audio = std::make_unique<Playout>(44100, targetMs);
		uint64_t next = 0;
		int pending = 44100 * targetMs / 1000 + 256;
		float output[2];
		for (int callback = 0; callback < 3000; ++callback)
		{
			pending += 441;
			const bool held = callback % 100 >= 50 && callback % 100 < 62;
			if (!held)
				while (pending >= 128)
				{
					Check(audio->Push(Block(next)), "jitter burst overflow");
					next += 128;
					pending -= 128;
				}
			audio->BeginCallback();
			for (int frame = 0; frame < 441; ++frame)
				audio->Frame(output);
		}
		Check(audio->lostFrames == 0 && audio->overflowFrames == 0, "jitter misclassified as packet loss");
		return audio->underruns.load();
	};
	Check(burstUnderruns(80) > 0, "jitter test did not exercise starvation");
	Check(burstUnderruns(150) == 0, "Wi-Fi burst exceeds default jitter protection");
	std::cout << "MTU, watermark, heartbeat, ArtDmx, SPSC stress, loss, overload and clock drift passed\n";
}
