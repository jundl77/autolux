#pragma once

#include <config/config.h>
#include <core/event_loop.h>
#include <network/udp_client.h>
#include <soundio/soundio.h>
#include "audio_delay.h"

#include <string>
#include <memory>

namespace DmxEnttecNode {

class AudioSender
{
public:
	AudioSender(const Config&, EventLoop& loop);
	~AudioSender();

	void Start();
	void SendAudioBytes(char* data, size_t size);
	void ResetAudioStream(const std::string& error);

private:
	void StartAudioRecorder();

	const Config& mConfig;
	EventLoop& mEventLoop;
	UdpClient mUdpClient;

	SoundIo* mSoundIo {nullptr};
	SoundIoDevice* mInputDevice {nullptr};
	SoundIoInStream* mInStream {nullptr};
	AudioDelay mDelay;
	uint64_t mSeqNum = 0;
};

}
