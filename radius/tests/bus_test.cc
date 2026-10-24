#include <proto/bus.h>
#include <proto/dmx_state.h>
#include <proto/artdmx.h>
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
	constexpr uint64_t Session = UINT64_MAX;
	auto packet = Bus::NewEnvelope(Session);
	packet.set_audio_frame_watermark(UINT64_MAX - Bus::AudioFrames);
	packet.set_sequence(UINT32_MAX);
	auto* audio = packet.mutable_audio();
	audio->set_sample_rate_hz(Bus::SampleRateHz);
	audio->set_channel_count(Bus::Channels);
	const std::array<float, 4> extremes = {std::numeric_limits<float>::max(), std::numeric_limits<float>::lowest(),
	                                       std::numeric_limits<float>::denorm_min(), -0.0f};
	for (size_t i = 0; i < Bus::AudioFrames * Bus::Channels; ++i)
		audio->add_interleaved_samples(extremes[i % extremes.size()]);
	std::array<uint8_t, Bus::MaxDatagramBytes> wire{};
	const auto size = Bus::Encode(packet, wire);
	Check(size > 0 && size <= Bus::MaxDatagramBytes, "audio MTU budget");
	Bus::Envelope decoded;
	Check(Bus::Decode(wire.data(), size, Session, decoded), "protobuf audio round trip");
	Check(decoded.session_id() == Session && decoded.audio_frame_watermark() == packet.audio_frame_watermark() &&
	          decoded.sequence() == UINT32_MAX,
	      "exact 64-bit identifiers and sequence");
	Check(decoded.audio().sample_rate_hz() == Bus::SampleRateHz && decoded.audio().channel_count() == Bus::Channels &&
	          decoded.audio().interleaved_samples_size() == Bus::AudioFrames * Bus::Channels,
	      "audio format and sample count");
	Check(std::memcmp(decoded.audio().interleaved_samples().data(), audio->interleaved_samples().data(),
	                  Bus::AudioFrames * Bus::AudioBytesPerFrame) == 0,
	      "IEEE float samples preserve bits");
	for (size_t n = 0; n < size; ++n)
		Check(!Bus::Decode(wire.data(), n, Session, decoded), "truncated protobuf accepted");
	Check(!Bus::Decode(wire.data(), size, Session - 1, decoded), "wrong session accepted");
	std::array<uint8_t, Bus::MaxDatagramBytes + 1> oversized{};
	Check(!Bus::Decode(oversized.data(), oversized.size(), Session, decoded), "oversize datagram accepted");
	const auto header = Bus::NewEnvelope(Session).SerializeAsString();
	const auto unknownPayload = header + std::string("\x72\0", 2); // Unknown oneof field 14, empty message.
	Check(!Bus::Decode(unknownPayload.data(), unknownPayload.size(), Session, decoded), "unknown kind accepted");
	const auto malformedLength = header + std::string("\x52\xff\xff\xff\xff\x07", 6);
	Check(!Bus::Decode(malformedLength.data(), malformedLength.size(), Session, decoded), "malformed length accepted");
	const auto rejects = [&](const Bus::Envelope& invalid) {
		Check(Bus::Encode(invalid, wire) == 0, "invalid outgoing message accepted");
		const auto raw = invalid.SerializeAsString();
		Check(!Bus::Decode(raw.data(), raw.size(), Session, decoded), "invalid incoming message accepted");
	};
	packet.set_protocol_version(Bus::ProtocolVersion + 1);
	rejects(packet);
	packet.set_protocol_version(Bus::ProtocolVersion);
	packet.set_session_id(0);
	rejects(packet);
	packet.set_session_id(Session);
	packet.set_audio_frame_watermark(UINT64_MAX - Bus::AudioFrames + 1);
	rejects(packet);
	packet.set_audio_frame_watermark(0);
	audio->set_sample_rate_hz(Bus::SampleRateHz + 1);
	rejects(packet);
	audio->set_sample_rate_hz(Bus::SampleRateHz);
	audio->set_channel_count(Bus::Channels + 1);
	rejects(packet);
	audio->set_channel_count(Bus::Channels);
	audio->set_interleaved_samples(0, std::numeric_limits<float>::quiet_NaN());
	rejects(packet);
	audio->set_interleaved_samples(0, std::numeric_limits<float>::infinity());
	rejects(packet);
	audio->set_interleaved_samples(0, 0);
	audio->add_interleaved_samples(0);
	rejects(packet);
	audio->mutable_interleaved_samples()->Clear();
	rejects(packet);
	audio->add_interleaved_samples(0);
	rejects(packet);
	packet.clear_payload();
	rejects(packet);
	packet.mutable_heartbeat();
	rejects(packet); // Media sequence is invalid on a heartbeat.
	packet.set_sequence(0);
	Check(Bus::Encode(packet, wire) > 0, "heartbeat payload presence");
	packet.set_audio_frame_watermark(UINT64_MAX);
	std::string dmx(DmxFrameSize, '\0');
	for (size_t channel = 0; channel < dmx.size(); ++channel)
		dmx[channel] = static_cast<char>(channel);
	packet.mutable_lighting()->set_channels(dmx);
	const auto lightingSize = Bus::Encode(packet, wire);
	Check(lightingSize && Bus::Decode(wire.data(), lightingSize, Session, decoded) &&
	          decoded.audio_frame_watermark() == UINT64_MAX && decoded.lighting().channels() == dmx,
	      "lighting bytes and maximum watermark");
	packet.mutable_lighting()->set_channels(dmx.substr(1));
	rejects(packet);
	packet.mutable_lighting()->set_channels(dmx + '\0');
	rejects(packet);

	packet = Bus::NewEnvelope(Session);
	auto& health = *packet.mutable_receiver_telemetry();
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
	auto telemetrySize = Bus::Encode(packet, wire);
	Check(telemetrySize && Bus::Decode(wire.data(), telemetrySize, Session, decoded), "telemetry round trip");
	auto json = Bus::ReceiverTelemetryToJson(decoded.receiver_telemetry());
	Check(json.size() == 24 && json["up"] == true && json["link_up"] == true && json["dmx_connected"] == false,
	      "dashboard health schema");
	Check(json["audio_input_age_seconds"].is_null() && json["audio_max_gap_seconds"].is_null(),
	      "missing input stays unavailable");
	for (const auto* key : {"heartbeats_sent", "heartbeats_received", "underruns_total", "device_underruns_total",
	                        "concealed_frames_total", "lost_frames_total", "overflow_frames_total",
	                        "late_packets_total", "latency_trims_total", "dmx_frames_total", "dmx_dropped_total",
	                        "invalid_packets_total", "send_failures_total"})
		Check(json[key].is_number_unsigned() && json[key].get<uint64_t>() == UINT64_MAX,
		      "telemetry counters retain full uint64 range");
	Check(json["heartbeat_age_seconds"] == 0.5 && json["audio_buffer_seconds"] == 0.125 &&
	          json["buffer_target_seconds"] == 0.15 && json["clock_correction_ppm"] == -999 &&
	          json["dmx_lateness_seconds"] == 0.025 && json["audio_signal_peak"] == 0.75,
	      "telemetry units unchanged");
	health.set_audio_input_age_seconds(0);
	health.set_audio_max_gap_seconds(0);
	telemetrySize = Bus::Encode(packet, wire);
	Check(telemetrySize && Bus::Decode(wire.data(), telemetrySize, Session, decoded), "zero input age round trip");
	json = Bus::ReceiverTelemetryToJson(decoded.receiver_telemetry());
	Check(json["audio_input_age_seconds"] == 0 && json["audio_max_gap_seconds"] == 0,
	      "measured zero remains distinct from unavailable");
	health.set_audio_input_age_seconds(-1);
	rejects(packet);
	health.clear_audio_input_age_seconds();
	rejects(packet);
	health.clear_audio_max_gap_seconds();
	health.set_audio_signal_peak(std::numeric_limits<float>::quiet_NaN());
	rejects(packet);
	health.set_audio_signal_peak(0);
	health.set_buffer_target_seconds(0);
	rejects(packet);
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
	      "SoundSwitch ArtDmx");
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
	// Clearing an overlay must restore the current base without waiting for a new Art-Net frame.
	DmxState dmx;
	std::array<uint8_t, 512> base{};
	base[10] = 5;
	dmx.Base(DmxFrame(base));
	OverlayIdl::OverlayMessage overlay{};
	overlay.mNumOverlays = 1;
	overlay.mOverlays[0] = {10, 1};
	overlay.mDmxData[10] = 255;
	dmx.Overlay(overlay);
	Check(dmx.Compose()[10] == 255, "overlay application");
	overlay.mNumOverlays = 0;
	dmx.Overlay(overlay);
	Check(dmx.Compose()[10] == 5, "overlay release");
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
	std::cout << "MTU, watermark, heartbeat, overlay release, SPSC stress, loss, overload and clock drift passed\n";
}
