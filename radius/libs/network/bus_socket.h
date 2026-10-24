#pragma once
#include <winsock2.h>
#include <ws2tcpip.h>
#include <proto/bus.h>
#include <functional>
#include <string>

namespace Radius {

class BusSocket
{
	SOCKET mSocket = INVALID_SOCKET;
	sockaddr_in mPeer{};
	bool mReceiver = false, mConnected = false, mWsa = false;

public:
	uint64_t invalidPackets = 0, sendFailures = 0, receivedPackets = 0, sentPackets = 0;
	BusSocket() = default;
	~BusSocket();
	void Open(const std::string& bindAddress, const std::string& peer, uint16_t port, bool receiver);
	bool Send(const Bus::Packet&);
	void Poll(uint64_t session, const std::function<void(const Bus::Packet&)>&);
};

} // namespace Radius
