#pragma once
#include <config/config.h>
#include <core/spsc_queue.h>
#include <proto/radius.h>
#include <soundio/soundio.h>
#include "audio_delay.h"

namespace Radius {

class AudioCapture
{
	static constexpr size_t QueueCapacityBlocks = 128;
	const Config& mConfig;
	SoundIo* mIo = nullptr;
	SoundIoDevice* mDevice = nullptr;
	SoundIoInStream* mStream = nullptr;
	AudioDelay mDelay;
	SpscQueue<Bus::AudioBlock, QueueCapacityBlocks> mQueue;
	uint64_t mFrame = 0;
	bool mThreadConfigured = false;
	static void Read(SoundIoInStream*, int, int) noexcept;

public:
	std::atomic<uint64_t> watermark{0}, droppedFrames{0}, captureOverruns{0};
	std::atomic<int> error{0};

	explicit AudioCapture(const Config& config)
	    : mConfig(config)
	{
	}

	~AudioCapture();
	void Start();

	const Bus::AudioBlock* Front() const
	{
		return mQueue.Front();
	}

	void Pop()
	{
		mQueue.Pop();
	}

	size_t QueuedFrames() const
	{
		return mQueue.Size() * Bus::AudioFrames;
	}
};

} // namespace Radius
