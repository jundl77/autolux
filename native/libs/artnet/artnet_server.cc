#include "artnet_server.h"

#include <core/logger.h>
#include <core/throw_if.h>
#include <sys/types.h>
#include <fcntl.h>
#include <cstring>

#ifdef WIN32
#include "winsock.h"
#include "ws2tcpip.h"
#else
#include <unistd.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#endif

extern "C"
{
#include "libartnet/packets.h"
#include "libartnet/private.h"
#include "libartnet/artnet.h"
}

namespace DmxEnttecNode {

static const LogModule LM_ARTNET {"ARTNET_SERVER"};
static IArtnetHandler* sArtnetHandlerPtr = nullptr; // so that we have access from the C library
static int sPacketsReceivedCounter = 0;

namespace {

void ConfigureArtnetPollReplyTemplate(artnet_node& artnetNode, const std::string& localIp)
{
	node n = reinterpret_cast<node>(artnetNode);

	// localIp is the IP which the DMX sender will use to send data, so it should be the local network IP of the
	// machine this node runs on
	SI localIpObj;
	localIpObj.s_addr = inet_addr(localIp.c_str());
	memcpy(&n->ar_temp.ip, &localIpObj, sizeof(localIpObj));
}

}


ArtnetServer::ArtnetServer(const Config& config, EventLoop& eventLoop, IArtnetHandler& handler)
	: mConfig(config)
	, mEventLoop(eventLoop)
	, mHandler(handler)
{
	sArtnetHandlerPtr = &mHandler;
}

ArtnetServer::~ArtnetServer()
{
	if (mNode != NULL)
	{
		artnet_stop(mNode);
		artnet_destroy(mNode);
	}
}

void ArtnetServer::StartListening()
{
	std::optional<std::string> localIpAddress = FindIpAddress();
	THROW_IF(!localIpAddress, "could not find IP address");
	THROW_IF(localIpAddress == "127.0.0.1", "127.0.0.1 is not allowed as local IP address");
	const char* ip_addr = localIpAddress->c_str();

	int verbose = 0; // 0 is not verbose, 1 is verbose
	mNode = artnet_new(ip_addr, verbose);
	if (mNode == NULL)
	{
		LOG(LL_ERROR, LM_ARTNET, "unable to create artnet node, stopping.");
		exit(1);
	}

	artnet_set_short_name(mNode, "Enttec USB Pro");
	artnet_set_long_name(mNode, "ArtNet Enttec USB Pro Node");
	artnet_set_node_type(mNode, ARTNET_NODE);
	artnet_dump_config(mNode);

	artnet_set_firmware_handler(mNode, FirmwareHandler, NULL);
	artnet_set_handler(mNode, ARTNET_DMX_HANDLER, DmxHandler, NULL);

	int port_id = 0; // first port
	uint8_t universe = 0x01; // subnet 0, universe 3

	if (artnet_start(mNode) != ARTNET_EOK)
	{
		char* artnetError = artnet_strerror();
		LOG(LL_ERROR, LM_ARTNET, "unable to start artnet node, stopping, error=%s", artnetError);
		exit(1);
	}

	mNodeSd = artnet_get_sd(mNode);
	mTv.tv_sec = 0;
	mTv.tv_usec = 0;

#ifdef WIN32
	bool is_blocking = false;
	u_long flags = is_blocking ? 0 : 1;
	ioctlsocket(mNodeSd, FIONBIO, &flags);
#else
	fcntl(mNodeSd, F_SETFL, O_NONBLOCK);
#endif

	ConfigureArtnetPollReplyTemplate(mNode, *localIpAddress);

	LOG(LL_INFO, LM_ARTNET, "started listening on %s:%d", localIpAddress->c_str(), ARTNET_PORT);
	mListenSocketPollHandle = mEventLoop.AddPoller([this]() { PollSocket(); });
	mArtPollReplyHandle = mEventLoop.AddTimer(1s, [this]() { SendArtPollReply(); });
	mMetricsHandle = mEventLoop.AddTimer(5s, [this]() { ReportMetrics(); });
}

void ArtnetServer::PollSocket()
{
	FD_ZERO(&mReadFds);
	FD_SET(mNodeSd, &mReadFds);

	int rv = select(mNodeSd + 1, &mReadFds, NULL, NULL, &mTv);
	if (rv != 1)
	{
		return;  // no data on socket
	}

	LOG(LL_DEBUG, LM_ARTNET, "received data");
	int res = artnet_read(mNode, 0);
	if (res != ARTNET_EOK)
	{
		LOG(LL_WARN, LM_ARTNET, "unable to read received artnet message successfully");
	}
};

void ArtnetServer::SendArtPollReply()
{
	DEBUG_LOG(LL_DEBUG, LM_ARTNET, "sending art-poll reply (to keep connections alive)");
	artnet_send_poll_reply(mNode);
}

int ArtnetServer::FirmwareHandler(artnet_node n, int ubea, uint16_t *data, int length, void *d)
{
	LOG(LL_DEBUG, LM_ARTNET, "firmware handler got %d words", length);
	return 0;
}

int ArtnetServer::DmxHandler(artnet_node n, void* packet, void* data)
{
	DEBUG_LOG(LL_DEBUG, LM_ARTNET, "received dmx data");
	artnet_packet artnetPacket = static_cast<artnet_packet>(packet);

	THROW_IF(sizeof(artnetPacket->data.admx.data) != DmxFrameSize, "unexpected dmx frame size");
	const DmxFrame dmxChannels = DmxFrame(artnetPacket->data.admx.data);
	sArtnetHandlerPtr->OnDmxMessage(dmxChannels, artnetPacket->data.admx.universe);
	sPacketsReceivedCounter += 1;
	return 0;
}

std::string ArtnetServer::FindIpAddress()
{
#ifdef WIN32
	WSADATA wsaData;
	int error = WSAStartup(0x0202, &wsaData);
	THROW_IF(error, "unable to open WSA");
#endif

	int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
	THROW_IF(sock == -1, "could not open socket");

	sockaddr_in loopback;
	loopback.sin_family = AF_INET;
	loopback.sin_addr.s_addr = 1337; // can be any IP address
	loopback.sin_port = htons(9); // using debug port

	int res = connect(sock, reinterpret_cast<sockaddr*>(&loopback), sizeof(loopback));
	THROW_IF(res == -1, "error with connect");

	int addrlen = sizeof(loopback);
	res = getsockname(sock, reinterpret_cast<sockaddr*>(&loopback), &addrlen);
	THROW_IF(res == -1, "error with getsockname");

	char buf[INET_ADDRSTRLEN];
	PCSTR resInetNtop = inet_ntop(AF_INET, &loopback.sin_addr, buf, INET_ADDRSTRLEN);
	THROW_IF(resInetNtop == 0x0, "error with inet_ntop");

#ifdef WIN32
	closesocket(sock);
	WSACleanup();
#else
	close(sock);
#endif

	return std::string(buf, INET_ADDRSTRLEN);
}

void ArtnetServer::ReportMetrics()
{
	LOG(LL_INFO, LM_ARTNET, "received %d dmx packets in the last 5 sec", sPacketsReceivedCounter);
	sPacketsReceivedCounter = 0;
}

}