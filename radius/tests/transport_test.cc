#include <network/udp_server.h>
#include <network/bus_socket.h>
#include <core/clock.h>
#include <soundio/audio_utils.h>
#include <thread>
#include <iostream>

using namespace Radius;
using namespace std::chrono_literals;

void CheckLoginLifecycle();

void Check(bool condition)
{
	if (!condition)
		throw std::runtime_error("transport regression failed");
}

struct Receiver : IUdpServerHandler
{
	EventLoop& loop;
	std::vector<std::vector<char>> packets;

	explicit Receiver(EventLoop& value)
	    : loop(value)
	{
	}

	void OnData(const void* data, size_t size) override
	{
		auto bytes = static_cast<const char*>(data);
		packets.emplace_back(bytes, bytes + size);
		if (packets.size() == 2)
			loop.Stop();
	}
};

void CheckBusPackets()
{
	constexpr uint64_t RunId = UINT64_MAX;
	constexpr auto PollTimeout = 2s;
	BusSocket receiver;
	// Reserve an ephemeral local port before opening the receiver there.
	SOCKET raw = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	Check(raw != INVALID_SOCKET);
	sockaddr_in destination{};
	destination.sin_family = AF_INET;
	destination.sin_addr.s_addr = inet_addr("127.0.0.1");
	Check(bind(raw, reinterpret_cast<sockaddr*>(&destination), sizeof(destination)) == 0);
	int addressSize = sizeof(destination);
	Check(getsockname(raw, reinterpret_cast<sockaddr*>(&destination), &addressSize) == 0);
	closesocket(raw);
	receiver.Open("127.0.0.1", "127.0.0.1", ntohs(destination.sin_port), true);
	raw = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	Check(raw != INVALID_SOCKET);
	const auto send = [&](const std::string& data) {
		Check(sendto(raw, data.data(), static_cast<int>(data.size()), 0, reinterpret_cast<sockaddr*>(&destination),
		             sizeof(destination)) == data.size());
	};
	uint64_t accepted = 0;
	const auto handler = [&](const Bus::Message& packet) {
		Check(std::holds_alternative<Bus::Login>(packet));
		++accepted;
	};
	const auto wire = [](const Bus::Message& message) {
		std::array<uint8_t, Bus::MaxDatagramBytes> bytes{};
		const auto size = Bus::SerializeMessage(message, bytes);
		Check(size > 0);
		return std::string(reinterpret_cast<const char*>(bytes.data()), size);
	};
	Bus::Heartbeat heartbeat;
	heartbeat.set_run_id(RunId - 1);
	send(wire(heartbeat));
	heartbeat.set_run_id(RunId);
	send(wire(heartbeat)); // No media or heartbeat may claim a receiver before Login.
	Bus::LightFrame lighting;
	lighting.set_run_id(RunId);
	lighting.set_channels(std::string(DmxFrameSize, '\0'));
	send(wire(lighting));
	Bus::LoginReply reply;
	reply.set_command_id(Bus::LoginCommandId);
	reply.set_run_id(RunId);
	send(wire(reply)); // RX rejects server messages.
	send(std::string(Bus::MaxDatagramBytes + 1, '\0'));
	send(std::string(Bus::MaxDatagramBytes * 2, '\0'));
	constexpr uint64_t InvalidPackets = 6;
	auto deadline = std::chrono::steady_clock::now() + PollTimeout;
	while (receiver.invalidPackets < InvalidPackets && std::chrono::steady_clock::now() < deadline)
	{
		receiver.Poll(RunId, handler);
		std::this_thread::sleep_for(1ms);
	}
	Check(receiver.invalidPackets == InvalidPackets && accepted == 0);
	// Invalid input must not claim the peer's port; a different, valid TX can still connect.
	BusSocket transmitter;
	transmitter.Open("127.0.0.1", "127.0.0.1", ntohs(destination.sin_port), false);
	Bus::Login login;
	login.set_command_id(Bus::LoginCommandId);
	login.set_run_id(RunId);
	Check(transmitter.Send(login));
	deadline = std::chrono::steady_clock::now() + PollTimeout;
	while (accepted == 0 && std::chrono::steady_clock::now() < deadline)
	{
		receiver.Poll(RunId, handler);
		std::this_thread::sleep_for(1ms);
	}
	Check(accepted == 1 && receiver.receivedPackets == 1);
	send(wire(login)); // The valid session cannot be taken over from another source port.
	receiver.Poll(RunId, handler);
	Check(accepted == 1 && receiver.invalidPackets == InvalidPackets + 1);
	Check(receiver.Send(heartbeat));
	Check(receiver.Send(lighting)); // TX rejects the wrong direction's media.
	uint64_t replies = 0;
	deadline = std::chrono::steady_clock::now() + PollTimeout;
	while ((replies == 0 || transmitter.invalidPackets == 0) && std::chrono::steady_clock::now() < deadline)
	{
		transmitter.Poll(RunId, [&](const Bus::Message& packet) {
			Check(std::holds_alternative<Bus::Heartbeat>(packet));
			++replies;
		});
		std::this_thread::sleep_for(1ms);
	}
	Check(replies == 1 && transmitter.invalidPackets == 1);
	// Delayed media from a previous run must not reach the audio/DMX consumers,
	// even from the accepted sender's port and with otherwise valid payloads.
	Bus::AudioFrame audio;
	audio.set_run_id(RunId - 1);
	audio.set_audio_frame_position(UINT64_MAX - Bus::AudioFrames);
	audio.set_sequence_number(UINT32_MAX);
	audio.add_interleaved_samples(0.5f);
	audio.add_interleaved_samples(0.5f);
	lighting.set_run_id(RunId - 1);
	lighting.set_audio_frame_position(UINT64_MAX);
	lighting.set_sequence_number(UINT32_MAX);
	Check(transmitter.Send(audio) && transmitter.Send(lighting));
	audio.set_run_id(RunId);
	audio.set_audio_frame_position(0);
	audio.set_sequence_number(1);
	lighting.set_run_id(RunId);
	lighting.set_audio_frame_position(0);
	lighting.set_sequence_number(1);
	Check(transmitter.Send(audio) && transmitter.Send(lighting));
	unsigned audioFrames = 0, lightFrames = 0;
	deadline = std::chrono::steady_clock::now() + PollTimeout;
	while (audioFrames + lightFrames < 2 && std::chrono::steady_clock::now() < deadline)
	{
		receiver.Poll(RunId, [&](const Bus::Message& packet) {
			if (const auto* frame = std::get_if<Bus::AudioFrame>(&packet))
			{
				Check(frame->run_id() == RunId && frame->audio_frame_position() == 0 && frame->sequence_number() == 1);
				++audioFrames;
			}
			else if (const auto* frame = std::get_if<Bus::LightFrame>(&packet))
			{
				Check(frame->run_id() == RunId && frame->audio_frame_position() == 0 && frame->sequence_number() == 1);
				++lightFrames;
			}
			else
				Check(false);
		});
		std::this_thread::sleep_for(1ms);
	}
	Check(audioFrames == 1 && lightFrames == 1 && receiver.invalidPackets == InvalidPackets + 3);
	receiver.ReleasePeer(); // Rejected initialization permits a corrected client to log in.
	send(wire(login));
	receiver.Poll(RunId, handler);
	Check(accepted == 2);
	closesocket(raw);
}

int main()
{
	const auto captureError = AudioStreamError("audio capture", SoundIoErrorStreaming);
	Check(captureError.find("audio capture failed: unrecoverable streaming failure") != std::string::npos);
	Check(captureError.find("RDP audio redirection") != std::string::npos);
	Check(AudioStreamError("audio playback", SoundIoErrorNoMem).find("RDP") == std::string::npos);
	// Real UDP proves that a short datagram never exposes the stale buffer tail.
	EventLoop loop;
	Receiver receiver(loop);
	UdpServer server(LogModule{"TEST"}, loop, receiver, 4096);
	server.Start(0, PollingMode::Poll1ms);
	CheckBusPackets();
	CheckLoginLifecycle();
	SOCKET client = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	sockaddr_in destination{};
	destination.sin_family = AF_INET;
	destination.sin_port = htons(server.ListenPort());
	destination.sin_addr.s_addr = inet_addr("127.0.0.1");
	Check(sendto(client, "hello", 5, 0, reinterpret_cast<sockaddr*>(&destination), sizeof(destination)) == 5);
	Check(sendto(client, "x", 1, 0, reinterpret_cast<sockaddr*>(&destination), sizeof(destination)) == 1);
	closesocket(client);
	auto timeout = loop.AddTimer(2s, [&] {
		loop.Stop();
	});
	loop.Run(RunHot::No);
	Check(receiver.packets == std::vector<std::vector<char>>{{'h', 'e', 'l', 'l', 'o'}, {'x'}});

	EventLoop posted;
	int count = 0;
	auto deadline = posted.AddTimer(3s, [&] {
		posted.Stop();
	});
	std::thread producer([&] {
		for (int i = 0; i < 1000; ++i)
			posted.Post(0ns, [&] {
				// Posting inside a callback used to invalidate the active queue.
				posted.Post(0ns, [&] {
					if (++count == 1000)
						posted.Stop();
				});
			});
	});
	posted.Run(RunHot::No);
	producer.join();
	Check(count == 1000);
	EventLoop changed;
	ScopedHandler self, added;
	int selfCalls = 0, addedCalls = 0;
	self = changed.AddTimer(0ns, [&] {
		++selfCalls;
		self = ScopedHandler{};
		added = changed.AddTimer(0ns, [&] {
			++addedCalls;
			changed.Stop();
		});
	});
	changed.Run(RunHot::No);
	Check(selfCalls == 1 && addedCalls == 1);
	std::cout << "packet bounds, UDP lengths, buffer space and concurrent recovery passed\n";
}
