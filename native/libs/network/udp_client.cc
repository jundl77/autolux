#include "udp_client.h"
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

UdpClient::UdpClient(const LogModule& logModule, EventLoop& loop)
	: mLogModule(logModule)
	, mEventLoop(loop)
{
}

UdpClient::~UdpClient()
{
	if (mRunning)
	{
		closesocket(mSocket);
		WSACleanup();
	}
}

void UdpClient::Connect(const std::string& ipv4, uint32_t port)
{
	InitSocket(ipv4, port);
	mMetricsHandle = mEventLoop.AddTimer(5s, [this]() { ReportMetrics(); });
}

void UdpClient::InitSocket(const std::string& ipv4, uint32_t port)
{
	LOG(LL_INFO, mLogModule, "creating socket for %s:%d..", ipv4.c_str(), port);

	int error = WSAStartup(0x0202, &mWsaData);
	THROW_IF(error, "unable to open WSA");

	mSocket = socket (AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	if (mSocket == INVALID_SOCKET)
	{
		WSACleanup();
		THROW_IF(true, "unable to open socket");
	}

	mServerAddr.sin_family = AF_INET;
	mServerAddr.sin_port = htons (port);
	mServerAddr.sin_addr.s_addr = inet_addr(ipv4.c_str());

	LOG(LL_INFO, mLogModule, "created socket for %s:%d successfully", ipv4.c_str(), port);
	mRunning = true;
}

void UdpClient::SendData(const void* data, size_t len)
{
	const char* dataBuf = static_cast<const char*>(data);
	sendto(mSocket, dataBuf, len, 0, (SOCKADDR*)&mServerAddr, sizeof(mServerAddr));
	mSentPacketsCounter += 1;
	mSentBytesCounter += len;
	DEBUG_LOG(LL_DEBUG, mLogModule, "sent message to %s with size %d", inet_ntoa(mServerAddr.sin_addr), len);
}

void UdpClient::ReportMetrics()
{
	LOG(LL_INFO, mLogModule, "data sent in the last 5 sec: packets=%d, bytes=%d",
		mSentPacketsCounter, mSentBytesCounter);
	mSentPacketsCounter = 0;
	mSentBytesCounter = 0;
}

}