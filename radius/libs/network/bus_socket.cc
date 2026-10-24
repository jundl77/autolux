#include "bus_socket.h"
#include <stdexcept>

namespace Radius {

constexpr int SocketBufferBytes = 1024 * 1024;
constexpr int MaxBusDatagramsPerPoll = 512;

BusSocket::~BusSocket()
{
	if (mSocket != INVALID_SOCKET)
		closesocket(mSocket);
	if (mWsa)
		WSACleanup();
}

void BusSocket::Open(const std::string& bindAddress, const std::string& peer, uint16_t port, bool receiver)
{
	WSADATA wsa{};
	if (WSAStartup(MAKEWORD(2, 2), &wsa))
		throw std::runtime_error("WSA startup failed");
	mWsa = true;
	mReceiver = receiver;
	mPeer.sin_family = AF_INET;
	mPeer.sin_port = htons(port);
	if (inet_pton(AF_INET, peer.c_str(), &mPeer.sin_addr) != 1)
		throw std::runtime_error("peer_address must be IPv4");
	sockaddr_in local{};
	local.sin_family = AF_INET;
	local.sin_port = htons(receiver ? port : 0);
	if (inet_pton(AF_INET, bindAddress.c_str(), &local.sin_addr) != 1)
		throw std::runtime_error("bind_address must be IPv4");
	mSocket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (mSocket == INVALID_SOCKET)
		throw std::runtime_error("bus socket failed");
	const int buffer = SocketBufferBytes;
	if (setsockopt(mSocket, SOL_SOCKET, SO_RCVBUF, reinterpret_cast<const char*>(&buffer), sizeof(buffer)) ||
	    setsockopt(mSocket, SOL_SOCKET, SO_SNDBUF, reinterpret_cast<const char*>(&buffer), sizeof(buffer)))
		throw std::runtime_error("bus socket buffer setup failed");
	u_long nonblocking = 1;
	if (ioctlsocket(mSocket, FIONBIO, &nonblocking) ||
	    bind(mSocket, reinterpret_cast<sockaddr*>(&local), sizeof(local)))
		throw std::runtime_error("bus socket bind failed");
	mConnected = !receiver;
}

bool BusSocket::Send(const Bus::Envelope& packet)
{
	if (!mConnected)
		return false;
	std::array<uint8_t, Bus::MaxDatagramBytes> bytes{};
	const auto size = Bus::SerializeEnvelope(packet, bytes);
	if (!size || sendto(mSocket, reinterpret_cast<const char*>(bytes.data()), static_cast<int>(size), 0,
	                    reinterpret_cast<const sockaddr*>(&mPeer), sizeof(mPeer)) != size)
	{
		++sendFailures;
		return false;
	}
	++sentPackets;
	return true;
}

void BusSocket::Poll(uint64_t session, const std::function<void(const Bus::Envelope&)>& handler)
{
	// Drain bursts instead of reading one datagram per Windows scheduler tick.
	for (int i = 0; i < MaxBusDatagramsPerPoll; ++i)
	{
		std::array<uint8_t, Bus::MaxDatagramBytes + 1> bytes{};
		sockaddr_in source{};
		int sourceSize = sizeof(source);
		const int size = recvfrom(mSocket, reinterpret_cast<char*>(bytes.data()), static_cast<int>(bytes.size()), 0,
		                          reinterpret_cast<sockaddr*>(&source), &sourceSize);
		if (size == SOCKET_ERROR)
		{
			const int error = WSAGetLastError();
			if (error == WSAEWOULDBLOCK)
				break;
			if (error == WSAEMSGSIZE || error == WSAECONNRESET)
			{
				++invalidPackets;
				continue;
			}
			throw std::runtime_error("bus receive failed: " + std::to_string(error));
		}
		Bus::Envelope packet;
		if (source.sin_addr.s_addr != mPeer.sin_addr.s_addr || (mConnected && source.sin_port != mPeer.sin_port) ||
		    !Bus::ParseEnvelope(bytes.data(), size, session, packet) ||
		    (mReceiver && packet.has_receiver_telemetry()) ||
		    (!mReceiver && (packet.has_audio() || packet.has_lighting())))
		{
			++invalidPackets;
			continue;
		}
		if (mReceiver && !mConnected)
		{
			mPeer = source;
			mConnected = true;
		}
		++receivedPackets;
		handler(packet);
	}
}

} // namespace Radius
