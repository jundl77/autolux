#include "usb_client.h"
#include "libenttec/usb_pro.h"
#include <core/logger.h>
#include <core/logger.h>
#include <core/throw_if.h>
#include <string>

namespace DmxEnttecNode {

static const LogModule LM_USB_CLIENT {"USB_CLIENT"};
static const WORD sVID = 0x0403;
static const WORD sPID = 0x6001;
static constexpr uint8_t DMX_PACKET_START_CODE = 0;

EnttecUSBClient::EnttecUSBClient(EventLoop& loop)
	: mEventLoop(loop)
{
}

EnttecUSBClient::~EnttecUSBClient()
{
	FTDI_PurgeBuffer();
	FTDI_ClosePort();
}

void EnttecUSBClient::Start()
{
	LOG(LL_INFO, LM_USB_CLIENT, "starting enttec USB client, looking for a PRO connected to PC ...");
	ReloadDriver();

	mNumDevices = FTDI_ListDevices();
	THROW_IF(mNumDevices != 1, "expected 1 enttec USB PRO device, found " + std::to_string(mNumDevices));
	LOG(LL_INFO, LM_USB_CLIENT, "found number of devices: %d", mNumDevices);

	mDeviceConnected = FTDI_OpenDevice(0); // device 0 is first device
	THROW_IF(!mDeviceConnected, "failed to open device for reading/writing");
	LOG(LL_INFO, LM_USB_CLIENT, "opened device successfully for reading/writing");

	mSenderHandle = mEventLoop.AddTimer(15ms, [this]() { FlushFrame(); });
	mMetricsHandle = mEventLoop.AddTimer(5s, [this]() { ReportMetrics(); });
}

void EnttecUSBClient::UpdateFrame(const DmxFrame& dmxFrame)
{
	// we need to shift the whole frame up by one to set DMX_PACKET_START_CODE
	std::memset(mSerializedFrame, 0, DmxFrameSize + 1);
	std::memcpy(mSerializedFrame + 1, dmxFrame.data(), dmxFrame.size());
	mSerializedFrame[0] = DMX_PACKET_START_CODE;
	if (mLastOverlayMsg)
	{
		ApplyOverlay(*mLastOverlayMsg, false);
	}
	mIsNewFrame = true;
}

void EnttecUSBClient::ApplyOverlay(const OverlayIdl::OverlayMessage& msg, bool bufferOverlay)
{
	for (int i = 0; i < msg.mNumOverlays; i++)
	{
		const int start = msg.mOverlays[i].mStart;
		const int length = msg.mOverlays[i].mLength;
		std::memcpy(mSerializedFrame + 1 + start, msg.mDmxData + start, length);
	}
	if (bufferOverlay)
	{
		mLastOverlayMsg = msg;
	}
	mIsNewFrame = true;
}

void EnttecUSBClient::FlushFrame()
{
	THROW_IF(!mDeviceConnected, "tried to send DMX to enttec USB PRO, but device is not connected");

	if (!mIsNewFrame)
	{
		return;
	}

	static_assert(sizeof(mSerializedFrame) == 513);
	int res = FTDI_SendData(SET_DMX_TX_MODE, mSerializedFrame, sizeof(mSerializedFrame));
	if (res < 0)
	{
		LOG(LL_WARN, LM_USB_CLIENT, "failed to send DMX to enttec USB PRO device");
		FTDI_ClosePort();
		return;
	}
	mSendCounter += 1;
	mIsNewFrame = false;
	DEBUG_LOG(LL_DEBUG, LM_USB_CLIENT, "sent DMX frame to USB device successfully, frame: %s", HexToString<513>(mSerializedFrame).c_str());
}

void EnttecUSBClient::ReloadDriver()
{
	LOG(LL_INFO, LM_USB_CLIENT, "reloading devices for use with drivers (wait 3.5sec)");
	mFtStatus = FT_Reload(sVID, sPID);
	Sleep(3500);
	if (mFtStatus != FT_OK)
	{
		LOG(LL_INFO, LM_USB_CLIENT, "reloading D2XX driver failed");
	}
	else
	{
		LOG(LL_INFO, LM_USB_CLIENT, "reloading D2XX driver passed");
	}
}

void EnttecUSBClient::ReportMetrics()
{
	LOG(LL_INFO, LM_USB_CLIENT, "sent %d dmx frames in the last 5 sec", mSendCounter);
	mSendCounter = 0;
}

}