#pragma once
#include "artnet_handler.h"
#include <config/config.h>
#include <network/udp_server.h>
namespace Radius {
class ArtnetServer : private IUdpServerHandler {
    const Config& mConfig;
    IArtnetHandler& mHandler;
    UdpServer mSocket;
    void OnData(const void*,size_t) override;
public:
    ArtnetServer(const Config&,EventLoop&,IArtnetHandler&);
    void StartListening();
};
}
