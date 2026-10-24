#pragma once
#include <network/bus_socket.h>
#include <network/link_state.h>
#include <core/app.h>
#include <core/health_file.h>
#include <artnet/artnet_server.h>
#include <overlay_server/overlay_server.h>
#include <proto/dmx_state.h>
#include <soundio/audio_capture.h>

namespace Radius {

class RadiusTx : public App, private IArtnetHandler, private IOverlayHandler
{
	const Config& mConfig;
	EventLoop& mLoop;
	BusSocket mBus;
	LinkState mLink{SteadyMs()};
	AudioCapture mAudio;
	ArtnetServer mArtnet;
	OverlayServer mOverlay;
	DmxState mDmx;
	njson mReceiver;
	uint64_t mReceiverAt = 0, mDmxAt = 0, mHealthAt = 0, mLastDmxSend = 0, mHeartbeatsSent = 0, mHeartbeatsReceived = 0;
	uint32_t mAudioSequence = 0, mDmxSequence = 0;
	ScopedHandler mPoll;
	void Tick();
	void OnDmxMessage(const DmxFrame&, int) override;
	void OnOverlayMessage(const OverlayIdl::OverlayMessage&) override;

public:
	RadiusTx(const Config& c, EventLoop& loop)
	    : mConfig(c)
	    , mLoop(loop)
	    , mAudio(c)
	    , mArtnet(c, loop, *this)
	    , mOverlay(c, loop, *this)
	{
	}

	void Start() override;

	const Config& GetConfig() const override
	{
		return mConfig;
	}
};

} // namespace Radius
