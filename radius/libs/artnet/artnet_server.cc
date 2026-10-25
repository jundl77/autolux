#include "artnet_server.h"
#include <bus/artdmx.h>

namespace Radius {

constexpr size_t ArtnetReceiveBufferBytes = 2048;

ArtnetServer::ArtnetServer(const Config& c, EventLoop& loop, IArtnetHandler& handler)
    : mConfig(c)
    , mHandler(handler)
    , mSocket(LogModule{"ARTNET"}, loop, *this, ArtnetReceiveBufferBytes)
{
}

void ArtnetServer::StartListening()
{
	mSocket.Start(mConfig.mArtnetPort, PollingMode::HotPoll, mConfig.mArtnetAddress,
	              mConfig.mArtnetPort == DefaultArtnetPort);
}

void ArtnetServer::OnData(const void* data, size_t size)
{
	std::array<uint8_t, DmxFrameSize> frame{};
	uint16_t universe = 0;
	if (DecodeArtDmx(data, size, frame, universe))
		mHandler.OnDmxMessage(DmxFrame(frame), universe);
}

} // namespace Radius
