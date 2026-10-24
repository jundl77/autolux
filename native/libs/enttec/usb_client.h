#pragma once

#include "enttec/libenttec/ftd2xx/ftd2xx.h"
#include <proto/overlay_proto.h>
#include <core/types.h>
#include <core/event_loop.h>
#include <core/scoped_handler.h>
#include <cstdint>
#include <optional>

namespace DmxEnttecNode {

class EnttecUSBClient
{
public:
	explicit EnttecUSBClient(EventLoop&);
	~EnttecUSBClient();

	void Start();
	void UpdateFrame(const DmxFrame&);
	void ApplyOverlay(const OverlayIdl::OverlayMessage& msg, bool bufferOverlay);

private:
	void FlushFrame();
	void ReloadDriver();
	void ReportMetrics();

	EventLoop& mEventLoop;
	FT_STATUS mFtStatus;
	uint8_t mNumDevices = 0;
	uint16_t mDeviceConnected = 0;

	uint8_t mSerializedFrame[DmxFrameSize + 1] {};
	std::optional<OverlayIdl::OverlayMessage> mLastOverlayMsg;
	bool mIsNewFrame = false;
	int mSendCounter = 0;

	ScopedHandler mSenderHandle;
	ScopedHandler mMetricsHandle;
};

}