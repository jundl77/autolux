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

int main()
{
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
	Bus::Packet packet, decoded;
	packet.kind = Bus::Kind::Audio;
	packet.size = 1024;
	packet.session = 0xf0123456789abcde;
	packet.watermark = 0x123456789abcdef0;
	std::array<uint8_t, Bus::MaxDatagram> wire{};
	const auto size = Bus::Encode(packet, wire);
	Check(size < 1200, "MTU budget");
	Check(Bus::Decode(wire.data(), size, decoded), "packet round trip");
	Check(decoded.watermark == packet.watermark && decoded.session == packet.session, "64-bit identifiers");
	for (size_t n = 0; n < size; ++n)
		Check(!Bus::Decode(wire.data(), n, decoded), "truncated packet accepted");
	wire[6] = 0xff;
	wire[7] = 0xff;
	Check(!Bus::Decode(wire.data(), size, decoded), "oversized payload");
	Bus::Encode(packet, wire);
	wire[28] = 1;
	Check(!Bus::Decode(wire.data(), size, decoded), "reserved header");
	packet.watermark = UINT64_MAX;
	Bus::Encode(packet, wire);
	Check(!Bus::Decode(wire.data(), size, decoded), "wrapping watermark");
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
