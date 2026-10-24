#include "artnet_server.h"
#include <proto/artdmx.h>
namespace Radius {
ArtnetServer::ArtnetServer(const Config& c,EventLoop& loop,IArtnetHandler& handler)
    :mConfig(c),mHandler(handler),mSocket(LogModule{"ARTNET"},loop,*this,2048) {}
void ArtnetServer::StartListening() {
    mSocket.Start(6454,PollingMode::HotPoll,mConfig.mArtnetAddress,true);
}
void ArtnetServer::OnData(const void* data,size_t size) {
    std::array<uint8_t,512> frame{}; uint16_t universe=0;
    if(DecodeArtDmx(data,size,frame,universe)) mHandler.OnDmxMessage(DmxFrame(frame),universe);
}
}
