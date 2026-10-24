#include "audio_receiver.h"
#include "audio_utils.h"
#include <proto/soundio_proto.h>
#include <core/logger.h>
#include <core/throw_if.h>
#include <soundio/soundio.h>
#include <string>

namespace DmxEnttecNode {

#undef min

namespace {

const LogModule LM_SENDER {"AUDIO_RECEIVER"};

AudioReceiver* sAudioReceiver {nullptr};
const double sMicrophoneLatencySec = 0.05;

void WriteCallback(SoundIoOutStream* outstream, int frameCountMin, int frameCountMax)
{
	SoundIoChannelArea *areas;
	int framesLeft;
	int frameCount;
	int err;

	SoundIoRingBuffer* ringBuffer = sAudioReceiver->GetRingBuffer();
	char* readPtr = soundio_ring_buffer_read_ptr(ringBuffer);
	int fillBytes = soundio_ring_buffer_fill_count(ringBuffer);
	int fillCount = fillBytes / outstream->bytes_per_frame;

	if (frameCountMin > fillCount)
	{
		// Ring buffer does not have enough data, fill with zeroes.
		framesLeft = frameCountMin;
		for (;;)
		{
			frameCount = framesLeft;
			if (frameCount <= 0)
				return;
			THROW_IF((err = soundio_outstream_begin_write(outstream, &areas, &frameCount)),
					 "begin write error: " + std::string(soundio_strerror(err)));
			if (frameCount <= 0)
				return;
			for (int frame = 0; frame < frameCount; frame += 1)
			{
				for (int ch = 0; ch < outstream->layout.channel_count; ch += 1)
				{
					memset(areas[ch].ptr, 0, outstream->bytes_per_sample);
					areas[ch].ptr += areas[ch].step;
				}
			}
			THROW_IF((err = soundio_outstream_end_write(outstream)),
					 "end write error: " + std::string(soundio_strerror(err)));
			framesLeft -= frameCount;
		}
	}

	int readCount = std::min(frameCountMax, fillCount);
	framesLeft = readCount;

	while (framesLeft > 0)
	{
		int frameCount = framesLeft;
		THROW_IF((err = soundio_outstream_begin_write(outstream, &areas, &frameCount)),
				 "begin write error: " + std::string(soundio_strerror(err)));

		if (frameCount <= 0)
			break;

		for (int frame = 0; frame < frameCount; frame += 1)
		{
			for (int ch = 0; ch < outstream->layout.channel_count; ch += 1)
			{
				memcpy(areas[ch].ptr, readPtr, outstream->bytes_per_sample);
				areas[ch].ptr += areas[ch].step;
				readPtr += outstream->bytes_per_sample;
			}
		}

		THROW_IF((err = soundio_outstream_end_write(outstream)),
				 "end write error: " + std::string(soundio_strerror(err)));
		framesLeft -= frameCount;
	}

	soundio_ring_buffer_advance_read_ptr(ringBuffer, readCount * outstream->bytes_per_frame);
}

void UnderflowCallback(SoundIoOutStream*)
{
	// we don't care about this
}

void ErrorCallback(SoundIoOutStream*, int err)
{
	std::string errorReason = std::string(soundio_strerror(err));
	sAudioReceiver->ResetAudioStream(errorReason);
}

}

AudioReceiver::AudioReceiver(const Config& config, EventLoop& loop)
	: mConfig(config)
	, mEventLoop(loop)
	, mUdpServer(LM_SENDER, mEventLoop, *this, SOUNDIO_MAX_DATA_SIZE * 2)
{
	THROW_IF(sAudioReceiver != nullptr, "only one audio-sender instance can be active");
	sAudioReceiver = this;

	mSoundIo = CreateSoundIo(DEFAULT_SOUNDIO_BACKEND);
}

AudioReceiver::~AudioReceiver()
{
	soundio_outstream_destroy(mOutStream);
	soundio_device_unref(mOutputDevice);
	soundio_destroy(mSoundIo);
	soundio_ring_buffer_destroy(mRingBuffer);
}

void AudioReceiver::Start()
{
	LogSupportedDevices(LM_SENDER, mSoundIo, false /*verbose*/, false /*logInputDevices*/, true /*logOutputDevices*/);
	mUdpServer.Start(mConfig.mAudioServerPort, PollingMode::Poll1ms);
	StartAudioPlayer();
}

void AudioReceiver::StartAudioPlayer()
{
	const bool isRawDevice = false; // a raw device directly interacts with the hardware
	mOutputDevice = GetAudioOutputDevice(mSoundIo, mConfig.mAudioDeviceId, isRawDevice);
	LOG(LL_INFO, LM_SENDER, "using audio output device: %s", mOutputDevice->name);
	LogSupportedFormats(LM_SENDER, mOutputDevice);
	LogSupportedSampleRates(LM_SENDER, mOutputDevice);
	LOG(LL_INFO, LM_SENDER, "using format '%s' and sample rate '%d'", mConfig.mAudioFormat.c_str(), mConfig.mAudioSampleRate);

	mOutStream = soundio_outstream_create(mOutputDevice);
	THROW_IF(!mOutStream, "could not setup soundio audio output, out of memory");
	mOutStream->format = SoundIoFormatFromString(mConfig.mAudioFormat);
	mOutStream->sample_rate = mConfig.mAudioSampleRate;
	mOutStream->software_latency = sMicrophoneLatencySec;
	mOutStream->write_callback = WriteCallback;
	mOutStream->underflow_callback = UnderflowCallback;
	mOutStream->error_callback = ErrorCallback;

	int err;
	THROW_IF((err = soundio_outstream_open(mOutStream)),
			 "unable to open output stream: " + std::string(soundio_strerror(err)));

	int capacity = sMicrophoneLatencySec * 64 * mConfig.mAudioSampleRate * mOutStream->bytes_per_frame;
	mRingBuffer = soundio_ring_buffer_create(mSoundIo, capacity);
	THROW_IF(!mRingBuffer, "unable to create ring buffer: out of memory");
	char* buf = soundio_ring_buffer_write_ptr(mRingBuffer);
	int fillCount = sMicrophoneLatencySec * mConfig.mAudioSampleRate * mOutStream->bytes_per_frame;
	memset(buf, 0, fillCount);
	soundio_ring_buffer_advance_write_ptr(mRingBuffer, fillCount);
	LOG(LL_INFO, LM_SENDER, "created ring-buffer with capacity %d", capacity);

	THROW_IF((err = soundio_outstream_start(mOutStream)),
			 "unable to start output device: " + std::string(soundio_strerror(err)));
	mAudioPlayerIsRunning = true;
}

void AudioReceiver::PlayAudioBytes(const char* data, size_t size)
{
	if (!mAudioPlayerIsRunning)
	{
		return;
	}

	LOG(LL_DEBUG, LM_SENDER, "ring-buffer state: capacity=%d, fill_count=%d, recv_data_size=%d",
		soundio_ring_buffer_capacity(mRingBuffer), soundio_ring_buffer_fill_count(mRingBuffer), size);
	if (size > soundio_ring_buffer_capacity(mRingBuffer))
	{
		return;
	}

	char *write_ptr = soundio_ring_buffer_write_ptr(mRingBuffer);
	std::memcpy(write_ptr, data, size);
	soundio_ring_buffer_advance_write_ptr(mRingBuffer, size);
}

void AudioReceiver::OnData(const void* data, size_t len)
{
	static int sSoundIoDataSize = sizeof(SoundIoIdl::SoundIoData);
	if (len < sSoundIoDataSize)
	{
		return;
	}

	int protocolId = 0;
	std::memcpy(&protocolId, data, sizeof(uint32_t));
	if (protocolId != SOUNDIO_PROTOCOL_ID)
	{
		return;
	}

	const SoundIoIdl::SoundIoData* soundData = static_cast<const SoundIoIdl::SoundIoData*>(data);
	THROW_IF(soundData->mProtocolId != SOUNDIO_PROTOCOL_ID, "protocol id is wrong");

	DEBUG_LOG(LL_DEBUG, LM_SENDER, "received sound data, seqnum=%d, size=%d", soundData->mSeqNum, soundData->mSize);
	PlayAudioBytes(soundData->mData, soundData->mSize);
}

void AudioReceiver::ResetAudioStream(const std::string& error)
{
	LOG(LL_INFO, LM_SENDER, "resetting audio stream in 2sec, reason=%s", error.c_str());
	mAudioPlayerIsRunning = false;
	mEventLoop.Post(2s, [this]()
	{
		LOG(LL_INFO, LM_SENDER, "resetting audio stream now");
		soundio_outstream_destroy(mOutStream);
		soundio_device_unref(mOutputDevice);
		soundio_destroy(mSoundIo);
		soundio_ring_buffer_destroy(mRingBuffer);
		mSoundIo = CreateSoundIo(DEFAULT_SOUNDIO_BACKEND);
		StartAudioPlayer();
	});
}

}