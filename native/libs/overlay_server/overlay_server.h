#pragma once

#include "overlay_handler.h"
#include <core/types.h>
#include <core/event_loop.h>
#include <network/udp_server.h>
#include <artnet/artnet_server.h>
#include <enttec/usb_client.h>

#include <cstdint>
#include <iostream>
#include <chrono>
#include <unordered_map>
#include <string>

namespace DmxEnttecNode {

class OverlayServer : private IUdpServerHandler
{
public:
	OverlayServer(const Config&, EventLoop& loop, IOverlayHandler&);
	~OverlayServer();

	void Start();

private:
	// IUdpServerHandler
	void OnData(const void*, size_t len) override;

private:
	const Config& mConfig;
	EventLoop& mEventLoop;
	IOverlayHandler& mHandler;
	UdpServer mUdpServer;
};

}