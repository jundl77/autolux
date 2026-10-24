#pragma once

#include "enttec/libenttec/ftd2xx/ftd2xx.h"
#include <core/types.h>
#include <core/event_loop.h>
#include <core/scoped_handler.h>
#include <cstdint>
#include <optional>

namespace Radius {

class EnttecUSBClient
{
public:
	explicit EnttecUSBClient(EventLoop&);
	~EnttecUSBClient();

	void Start();
	void UpdateFrame(const DmxFrame&);

private:
	void FlushFrame();
	void ReloadDriver();
	void ReportMetrics();

	EventLoop& mEventLoop;
	FT_STATUS mFtStatus;
	int mNumDevices = 0;
	uint16_t mDeviceConnected = 0;

	uint8_t mSerializedFrame[DmxFrameSize + 1]{};
	bool mIsNewFrame = false;
	int mSendCounter = 0;

	ScopedHandler mSenderHandle;
	ScopedHandler mMetricsHandle;
};

} // namespace Radius
