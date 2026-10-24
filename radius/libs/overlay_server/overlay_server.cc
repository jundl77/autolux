#include "overlay_server.h"
#include <proto/overlay_proto.h>
#include <proto/validation.h>
#include <core/logger.h>
#include <core/throw_if.h>
#include <core/types.h>
#include <iostream>
#include <string>

namespace Radius {

static const LogModule LM_OVERLAY{"OVERLAY"};
static const size_t sOverlayRecvBufferSize = 4096;

OverlayServer::OverlayServer(const Config& config, EventLoop& loop, IOverlayHandler& handler)
    : mConfig(config)
    , mEventLoop(loop)
    , mHandler(handler)
    , mUdpServer(LM_OVERLAY, mEventLoop, *this, sOverlayRecvBufferSize)
{
}

OverlayServer::~OverlayServer()
{
}

void OverlayServer::Start()
{
	mUdpServer.Start(mConfig.mOverlayListenPort, PollingMode::HotPoll);
}

void OverlayServer::OnData(const void* data, size_t len)
{
	OverlayIdl::OverlayMessage message;
	if (!DecodeOverlayPacket(data, len, message))
		return;

	DEBUG_LOG(LL_DEBUG, LM_OVERLAY, "received overlay message: %s", ToString(message).c_str());
	mHandler.OnOverlayMessage(message);
}

} // namespace Radius
