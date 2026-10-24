#include "artnet_server.h"
#include <proto/artdmx.h>

namespace Radius {

constexpr uint16_t ArtnetListenPort = 6454;
constexpr size_t ArtnetReceiveBufferBytes = 2048;

ArtnetServer::ArtnetServer(const Config& c, EventLoop& loop, IArtnetHandler& handler)
    : mConfig(c)
    , mHandler(handler)
    , mSocket(LogModule{"ARTNET"}, loop, *this, ArtnetReceiveBufferBytes)
{
}

void ArtnetServer::StartListening()
{
	mSocket.Start(ArtnetListenPort, PollingMode::HotPoll, mConfig.mArtnetAddress, true);
}

void ArtnetServer::OnData(const void* data, size_t size)
{
	std::array<uint8_t, DmxFrameSize> frame{};
	uint16_t universe = 0;
	if (DecodeArtDmx(data, size, frame, universe))
		mHandler.OnDmxMessage(DmxFrame(frame), universe);
}

} // namespace Radius
