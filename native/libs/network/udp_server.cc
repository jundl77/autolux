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

namespace DmxEnttecNode {

UdpServer::UdpServer(const LogModule& logModule,
					 EventLoop& loop,
					 IUdpServerHandler& handler,
					 size_t recvBufferSize)
	: mLogModule(logModule)
	, mEventLoop(loop)
	, mHandler(handler)
{
	mRecvBuffer.reserve(recvBufferSize);
	std::memset(mRecvBuffer.data(), 0, recvBufferSize);
}

UdpServer::~UdpServer()
{
	if (mRunning)
	{
		closesocket(mSocket);
		WSACleanup();
	}
}

void UdpServer::Start(uint32_t listenPort, PollingMode mode)
{
	StartServer(listenPort);
	switch (mode)
	{
		case PollingMode::HotPoll:
			mListenSocketPollHandle = mEventLoop.AddPoller([this]() { PollSocket(); });
			break;
		case PollingMode::Poll1ms:
			mListenSocketPollHandle = mEventLoop.AddTimer(1ms, [this]() { PollSocket(); });
			break;
	}
	mMetricsHandle = mEventLoop.AddTimer(5s, [this]() { ReportMetrics(); });
}

void UdpServer::StartServer(uint32_t listenPort)
{
	LOG(LL_INFO, mLogModule, "starting udp server on port %d..", listenPort);

	int error = WSAStartup(0x0202, &mWsaData);
	THROW_IF(error, "unable to open WSA");

	mSocket = socket (AF_INET, SOCK_DGRAM, IPPROTO_UDP);
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
	addr.sin_port = htons (listenPort);
	addr.sin_addr.s_addr = htonl (INADDR_ANY);

	if (bind(mSocket, (LPSOCKADDR)&addr, sizeof(addr)) == SOCKET_ERROR)
	{
		closesocket(mSocket);
		WSACleanup();
		THROW_IF(true, "unable to bind to socket");
	}

	LOG(LL_INFO, mLogModule, "server started on port %d", listenPort);
	mRunning = true;
}

void UdpServer::PollSocket()
{
	static int sClientAddrLen = sizeof(mClientAddr);
	int result = recvfrom(mSocket, mRecvBuffer.data(), mRecvBuffer.capacity(), 0, (sockaddr*)&mClientAddr, &sClientAddrLen);
	if (result == SOCKET_ERROR)
	{
		return;
	}

	DEBUG_LOG(LL_DEBUG, mLogModule, "received message from %s size %d", inet_ntoa(mClientAddr.sin_addr), mRecvBuffer.capacity());
	mReceivedPacketsCounter += 1;
	mReceivedBytesCounter += mRecvBuffer.capacity();
	mHandler.OnData(mRecvBuffer.data(), mRecvBuffer.capacity());
}

void UdpServer::ReportMetrics()
{
	LOG(LL_INFO, mLogModule, "data received in the last 5 sec: packets=%d, bytes=%d",
		mReceivedPacketsCounter, mReceivedBytesCounter);
	mReceivedPacketsCounter = 0;
	mReceivedBytesCounter = 0;
}

}