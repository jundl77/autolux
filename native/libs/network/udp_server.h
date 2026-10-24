#pragma once

#include <core/types.h>
#include <core/logger.h>
#include <core/event_loop.h>
#include <artnet/artnet_server.h>
#include <enttec/usb_client.h>

#include <cstdint>
#include <iostream>
#include <chrono>
#include <unordered_map>
#include <string>

namespace DmxEnttecNode {

enum class PollingMode
{
	HotPoll,
	Poll1ms
};

class IUdpServerHandler
{
public:
	virtual ~IUdpServerHandler() = default;
	virtual void OnData(const void* data, size_t len) = 0;
};

class UdpServer
{
public:
	UdpServer(const LogModule&, EventLoop& loop, IUdpServerHandler&, size_t recvBufferSize);
	~UdpServer();

	void Start(uint32_t listenPort, PollingMode);

private:
	void StartServer(uint32_t listenPort);
	void PollSocket();
	void ReportMetrics();

private:
	const LogModule mLogModule;
	EventLoop& mEventLoop;
	IUdpServerHandler& mHandler;

	WSADATA mWsaData;
	int mSocket;
	sockaddr_in mClientAddr;
	std::vector<char> mRecvBuffer;
	bool mRunning = false;
	uint32_t mReceivedPacketsCounter = 0;
	uint64_t mReceivedBytesCounter = 0;

	ScopedHandler mListenSocketPollHandle;
	ScopedHandler mMetricsHandle;
};

}