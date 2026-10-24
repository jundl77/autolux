#pragma once

#include "artnet_handler.h"
#include <config/config.h>
#include <core/event_loop.h>

extern "C"
{
#include "libartnet/artnet.h"
}

namespace DmxEnttecNode {

class ArtnetServer
{
public:
	explicit ArtnetServer(const Config&, EventLoop&, IArtnetHandler&);
	~ArtnetServer();

	void StartListening();

private:
	void PollSocket();
	std::string FindIpAddress();

	void SendArtPollReply();
	void ReportMetrics();

	// libartnet handlers
	static int FirmwareHandler(artnet_node n, int ubea, uint16_t *data, int length, void *d);
	static int DmxHandler(artnet_node n, void* packet, void* data);

private:
	const Config& mConfig;
	EventLoop& mEventLoop;
	IArtnetHandler& mHandler;
	ScopedHandler mListenSocketPollHandle;
	ScopedHandler mArtPollReplyHandle;
	ScopedHandler mMetricsHandle;

	artnet_node mNode {NULL};
	int mNodeSd;
	struct timeval mTv;
	fd_set mReadFds;
};

}