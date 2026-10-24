#pragma once

#include <config/config.h>
#include <core/event_loop.h>
#include <network/udp_server.h>
#include <soundio/soundio.h>

#include <string>
#include <memory>

namespace DmxEnttecNode {

class AudioReceiver : private IUdpServerHandler
{
public:
	AudioReceiver(const Config&, EventLoop& loop);
	~AudioReceiver();

	void Start();
	SoundIoRingBuffer* GetRingBuffer() { return mRingBuffer; };
	void ResetAudioStream(const std::string& error);

private:
	void StartAudioPlayer();
	void PlayAudioBytes(const char* data, size_t size);

	// IUdpServerHandler
	void OnData(const void*, size_t len) override;

	const Config& mConfig;
	EventLoop& mEventLoop;
	UdpServer mUdpServer;

	SoundIo* mSoundIo;
	SoundIoDevice* mOutputDevice;
	SoundIoOutStream* mOutStream;
	SoundIoRingBuffer* mRingBuffer;
	bool mAudioPlayerIsRunning {false};
};

}