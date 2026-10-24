#include <proto/validation.h>
#include <network/udp_server.h>
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

int main()
{
	Clock::Initialise();
	OverlayIdl::OverlayMessage overlay{}, parsed{};
	overlay.mNumOverlays = 1;
	overlay.mOverlays[0] = {0, 512};
	Check(DecodeOverlayPacket(&overlay, sizeof(overlay), parsed));
	Check(!DecodeOverlayPacket(&overlay, 7, parsed));
	overlay.mNumOverlays = 101;
	Check(!DecodeOverlayPacket(&overlay, sizeof(overlay), parsed));
	overlay.mNumOverlays = 1;
	overlay.mOverlays[0] = {511, 2};
	Check(!DecodeOverlayPacket(&overlay, sizeof(overlay), parsed));
	overlay.mOverlays[0] = {65535, 65535};
	Check(!DecodeOverlayPacket(&overlay, sizeof(overlay), parsed));

	// Real UDP proves that a short datagram never exposes the stale buffer tail.
	EventLoop loop;
	Receiver receiver(loop);
	UdpServer server(LogModule{"TEST"}, loop, receiver, 4096);
	server.Start(0, PollingMode::Poll1ms);
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
