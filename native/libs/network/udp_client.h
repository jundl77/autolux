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

class UdpClient
{
public:
	UdpClient(const LogModule&, EventLoop& loop);
	~UdpClient();

	void Connect(const std::string& ipv4, uint32_t port);
	void SendData(const void* data, size_t len);

private:
	void InitSocket(const std::string& ipv4, uint32_t port);
	void ReportMetrics();

private:
	const LogModule mLogModule;
	EventLoop& mEventLoop;

	WSADATA mWsaData;
	int mSocket;
	sockaddr_in mServerAddr;
	bool mRunning = false;
	uint32_t mSentPacketsCounter = 0;
	uint64_t mSentBytesCounter = 0;

	ScopedHandler mListenSocketPollHandle;
	ScopedHandler mMetricsHandle;
};

}