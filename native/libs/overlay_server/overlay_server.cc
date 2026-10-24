#include "overlay_server.h"
#include <proto/overlay_proto.h>
#include <core/logger.h>
#include <core/throw_if.h>
#include <core/types.h>
#include <iostream>
#include <string>

namespace DmxEnttecNode {

static const LogModule LM_OVERLAY {"OVERLAY"};
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
	static int sOverlayMessageSize = sizeof(OverlayIdl::OverlayMessage);
	if (len < sOverlayMessageSize)
	{
		return;
	}

	int protocolId = 0;
	std::memcpy(&protocolId, data, sizeof(uint32_t));
	if (protocolId != OVERLAY_PROTOCOL_ID)
	{
		return;
	}

	OverlayIdl::OverlayMessage message;
	std::memcpy(&message, data, sOverlayMessageSize);
	THROW_IF(message.mProtocolId != OVERLAY_PROTOCOL_ID, "protocol id is wrong");

	DEBUG_LOG(LL_DEBUG, LM_OVERLAY, "received overlay message: %s", ToString(message).c_str());
	mHandler.OnOverlayMessage(message);
}

}