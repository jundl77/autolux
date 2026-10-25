#include <network/udp_server.h>
#include <network/bus_socket.h>
#include <core/clock.h>
#include <thread>
#include <iostream>

using namespace Radius;
using namespace std::chrono_literals;

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
	constexpr uint64_t Session = UINT64_MAX;
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
	const auto handler = [&](const Bus::Envelope& packet) {
		Check(packet.has_heartbeat());
		++accepted;
	};
	auto invalid = Bus::NewEnvelope(Session - 1);
	invalid.mutable_heartbeat();
	send(invalid.SerializeAsString());
	invalid.set_session_id(Session);
	invalid.set_protocol_version(Bus::ProtocolVersion + 1);
	send(invalid.SerializeAsString());
	invalid.set_protocol_version(Bus::ProtocolVersion);
	invalid.clear_payload();
	send(invalid.SerializeAsString());
	auto* health = invalid.mutable_receiver_telemetry();
	health->set_up(true);
	health->set_buffer_target_seconds(0.15);
	send(invalid.SerializeAsString()); // RX must not accept telemetry from TX.
	send(std::string(Bus::MaxDatagramBytes + 1, '\0'));
	send(std::string(Bus::MaxDatagramBytes * 2, '\0'));
	constexpr uint64_t InvalidPackets = 6;
	auto deadline = std::chrono::steady_clock::now() + PollTimeout;
	while (receiver.invalidPackets < InvalidPackets && std::chrono::steady_clock::now() < deadline)
	{
		receiver.Poll(Session, handler);
		std::this_thread::sleep_for(1ms);
	}
	Check(receiver.invalidPackets == InvalidPackets && accepted == 0);
	// Invalid input must not claim the peer's port; a different, valid TX can still connect.
	BusSocket transmitter;
	transmitter.Open("127.0.0.1", "127.0.0.1", ntohs(destination.sin_port), false);
	auto heartbeat = Bus::NewEnvelope(Session);
	heartbeat.mutable_heartbeat();
	Check(transmitter.Send(heartbeat));
	deadline = std::chrono::steady_clock::now() + PollTimeout;
	while (accepted == 0 && std::chrono::steady_clock::now() < deadline)
	{
		receiver.Poll(Session, handler);
		std::this_thread::sleep_for(1ms);
	}
	Check(accepted == 1 && receiver.receivedPackets == 1);
	Check(receiver.Send(heartbeat));
	auto lighting = Bus::NewEnvelope(Session);
	lighting.mutable_lighting()->set_channels(std::string(DmxFrameSize, '\0'));
	Check(receiver.Send(lighting)); // TX rejects the wrong direction's media.
	uint64_t replies = 0;
	deadline = std::chrono::steady_clock::now() + PollTimeout;
	while ((replies == 0 || transmitter.invalidPackets == 0) && std::chrono::steady_clock::now() < deadline)
	{
		transmitter.Poll(Session, [&](const Bus::Envelope& packet) {
			Check(packet.has_heartbeat());
			++replies;
		});
		std::this_thread::sleep_for(1ms);
	}
	Check(replies == 1 && transmitter.invalidPackets == 1);
	closesocket(raw);
}

int main()
{
	// Real UDP proves that a short datagram never exposes the stale buffer tail.
	EventLoop loop;
	Receiver receiver(loop);
	UdpServer server(LogModule{"TEST"}, loop, receiver, 4096);
	server.Start(0, PollingMode::Poll1ms);
	CheckBusPackets();
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
