#include "udp_server.h"
#include <core/logger.h>
#include <core/throw_if.h>
#include <core/types.h>
#include <iostream>
#include <string>

#ifdef WIN32
#include <windows.h>
#include <winsock.h>
#endif

namespace Radius {

UdpServer::UdpServer(const LogModule& logModule, EventLoop& loop, IUdpServerHandler& handler, size_t recvBufferSize)
    : mLogModule(logModule)
    , mEventLoop(loop)
    , mHandler(handler)
{
	mRecvBuffer.resize(recvBufferSize);
}

UdpServer::~UdpServer()
{
	if (mRunning)
	{
		closesocket(mSocket);
		WSACleanup();
	}
}

void UdpServer::Start(uint32_t listenPort, PollingMode mode, const std::string& address, bool reuse)
{
	StartServer(listenPort, address, reuse);
	switch (mode)
	{
	case PollingMode::HotPoll:
		mListenSocketPollHandle = mEventLoop.AddPoller([this]() {
			PollSocket();
		});
		break;
	case PollingMode::Poll1ms:
		mListenSocketPollHandle = mEventLoop.AddTimer(1ms, [this]() {
			PollSocket();
		});
		break;
	}
	mMetricsHandle = mEventLoop.AddTimer(5s, [this]() {
		ReportMetrics();
	});
}

void UdpServer::StartServer(uint32_t listenPort, const std::string& address, bool reuse)
{
	LOG(LL_INFO, mLogModule, "starting udp server on port %d..", listenPort);

	int error = WSAStartup(0x0202, &mWsaData);
	THROW_IF(error, "unable to open WSA");

	mSocket = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (mSocket == INVALID_SOCKET)
	{
		WSACleanup();
		THROW_IF(true, "unable to open socket");
	}

	bool is_blocking = false;
	u_long flags = is_blocking ? 0 : 1;
	ioctlsocket(mSocket, FIONBIO, &flags);

	SOCKADDR_IN addr;
	addr.sin_family = AF_INET;
	addr.sin_port = htons(listenPort);
	if (inet_pton(AF_INET, address.c_str(), &addr.sin_addr) != 1)
	{
		closesocket(mSocket);
		WSACleanup();
		throw std::runtime_error("invalid UDP bind address");
	}
	if (reuse)
	{
		const int yes = 1;
		setsockopt(mSocket, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&yes), sizeof(yes));
	}

	if (bind(mSocket, (LPSOCKADDR)&addr, sizeof(addr)) == SOCKET_ERROR)
	{
		closesocket(mSocket);
		WSACleanup();
		THROW_IF(true, "unable to bind to socket");
	}

	LOG(LL_INFO, mLogModule, "server started on port %d", listenPort);
	mRunning = true;
}

uint16_t UdpServer::ListenPort() const
{
	sockaddr_in address{};
	int length = sizeof(address);
	THROW_IF(getsockname(mSocket, reinterpret_cast<sockaddr*>(&address), &length) == SOCKET_ERROR,
	         "unable to read bound UDP port");
	return ntohs(address.sin_port);
}

void UdpServer::PollSocket()
{
	for (int i = 0; i < 256; ++i)
	{
		int clientAddrLen = sizeof(mClientAddr);
		const int result = recvfrom(mSocket, mRecvBuffer.data(), static_cast<int>(mRecvBuffer.size()), 0,
		                            reinterpret_cast<sockaddr*>(&mClientAddr), &clientAddrLen);
		if (result == SOCKET_ERROR)
		{
			const int error = WSAGetLastError();
			if (error == WSAEWOULDBLOCK)
				break;
			if (error == WSAEMSGSIZE)
				continue;
			throw std::runtime_error("UDP input failed: " + std::to_string(error));
		}
		++mReceivedPacketsCounter;
		mReceivedBytesCounter += result;
		mHandler.OnData(mRecvBuffer.data(), static_cast<size_t>(result));
	}
}

void UdpServer::ReportMetrics()
{
	LOG(LL_INFO, mLogModule, "data received in the last 5 sec: packets=%u, bytes=%llu", mReceivedPacketsCounter,
	    mReceivedBytesCounter);
	mReceivedPacketsCounter = 0;
	mReceivedBytesCounter = 0;
}

} // namespace Radius
