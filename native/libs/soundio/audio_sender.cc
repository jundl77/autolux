#include "audio_sender.h"
#include "audio_utils.h"
#include <config/config.h>
#include <proto/soundio_proto.h>
#include <core/throw_if.h>
#include <core/logger.h>
#include <algorithm>
#include <string>

namespace DmxEnttecNode {

namespace {

const LogModule LM_SENDER {"AUDIO_SENDER"};

AudioSender* sAudioSender {nullptr};
const double sMicrophoneLatencySec = 0.05;


void ReadCallback(SoundIoInStream* instream, int frameCountMin, int frameCountMax)
{
	SoundIoChannelArea *areas;
	int err;
	int writeFrames = frameCountMax;
	int framesLeft = writeFrames;
	int maxReadSize = writeFrames * instream->bytes_per_frame;

	static std::vector<char> framesBuffer;
	framesBuffer.resize(maxReadSize);
	std::memset(framesBuffer.data(), 0, maxReadSize);
	int framesBufferIndex = 0;

	for (;;)
	{
		int frameCount = framesLeft;
		THROW_IF((err = soundio_instream_begin_read(instream, &areas, &frameCount)),
				 "begin read error: " + std::string(soundio_strerror(err)));

		if (!frameCount)
			break;

		if (!areas)
		{
			// Due to an overflow there is a hole. Fill the ring buffer with silence for the size of the hole.
			memset(framesBuffer.data() + framesBufferIndex, 0, frameCount * instream->bytes_per_frame);
			framesBufferIndex += frameCount * instream->bytes_per_frame;
			LOG(LL_ERROR, LM_SENDER, "Dropped %d frames due to internal overflow", frameCount);
		}
		else
		{
			for (int frame = 0; frame < frameCount; frame += 1)
			{
				for (int ch = 0; ch < instream->layout.channel_count; ch += 1)
				{
					memcpy(framesBuffer.data() + framesBufferIndex, areas[ch].ptr, instream->bytes_per_sample);
					areas[ch].ptr += areas[ch].step;
					framesBufferIndex += instream->bytes_per_sample;
				}
			}
		}

		THROW_IF((err = soundio_instream_end_read(instream)),
				 "end read error: " + std::string(soundio_strerror(err)));

		framesLeft -= frameCount;
		if (framesLeft <= 0)
			break; 
	}

	THROW_IF(framesBufferIndex > maxReadSize, "copied more bytes than we predicted, something is wrong");
	sAudioSender->SendAudioBytes(framesBuffer.data(), framesBufferIndex);
}

void ErrorCallback(SoundIoInStream*, int err)
{
	std::string errorReason = std::string(soundio_strerror(err));
	sAudioSender->ResetAudioStream(errorReason);
}

}

AudioSender::AudioSender(const Config& config, EventLoop& loop)
	: mConfig(config)
	, mEventLoop(loop)
	, mUdpClient(LM_SENDER, loop)
{
	THROW_IF(sAudioSender != nullptr, "only one audio-sender instance can be active");
	sAudioSender = this;

	mSoundIo = CreateSoundIo(DEFAULT_SOUNDIO_BACKEND);
}

AudioSender::~AudioSender()
{
	soundio_instream_destroy(mInStream);
	soundio_device_unref(mInputDevice);
	soundio_destroy(mSoundIo);
}

void AudioSender::Start()
{
	LogSupportedDevices(LM_SENDER, mSoundIo, false /*verbose*/, true /*logInputDevices*/, false /*logOutputDevices*/);
	mUdpClient.Connect(mConfig.mAudioServerIpv4, mConfig.mAudioServerPort);
	StartAudioRecorder();
}

void AudioSender::StartAudioRecorder()
{
	const bool isRawDevice = false; // a raw device directly interacts with the hardware
	mInputDevice = GetAudioInputDevice(mSoundIo, mConfig.mAudioDeviceId, isRawDevice);
	LOG(LL_INFO, LM_SENDER, "using audio input device: %s", mInputDevice->name);
	LogSupportedFormats(LM_SENDER, mInputDevice);
	LogSupportedSampleRates(LM_SENDER, mInputDevice);
	LOG(LL_INFO, LM_SENDER, "using format '%s' and sample rate '%d'", mConfig.mAudioFormat.c_str(), mConfig.mAudioSampleRate);

	mInStream = soundio_instream_create(mInputDevice);
	THROW_IF(!mInStream, "could not setup soundio audio input, out of memory");
	mInStream->format = SoundIoFormatFromString(mConfig.mAudioFormat);
	mInStream->sample_rate = mConfig.mAudioSampleRate;
	mInStream->software_latency = sMicrophoneLatencySec;
	mInStream->read_callback = ReadCallback;
	mInStream->error_callback = ErrorCallback;

	int err;
	THROW_IF((err = soundio_instream_open(mInStream)),
			 "unable to open input stream: " + std::string(soundio_strerror(err)));
	mDelay.Reset(mConfig.mPlaybackDelaySeconds, mInStream->sample_rate, mInStream->bytes_per_frame);
	LOG(LL_INFO, LM_SENDER, "audio delay: %.6f seconds (%zu frames); network receiver adds its normal device latency",
		mConfig.mPlaybackDelaySeconds, mDelay.Frames());
	THROW_IF((err = soundio_instream_start(mInStream)),
			 "unable to start input device: " + std::string(soundio_strerror(err)));
}

void AudioSender::SendAudioBytes(char* data, size_t size)
{
	mDelay.Process(data, size);
	const size_t packetCapacity = (SOUNDIO_MAX_DATA_SIZE / mInStream->bytes_per_frame) * mInStream->bytes_per_frame;
	static SoundIoIdl::SoundIoData soundioData {};
	while (size > 0)
	{
		const size_t packetSize = (std::min)(size, packetCapacity);
		soundioData.mSeqNum = ++mSeqNum;
		soundioData.mSize = static_cast<uint32_t>(packetSize);
		std::memcpy(soundioData.mData, data, packetSize);
		mUdpClient.SendData(&soundioData, sizeof(soundioData));
		data += packetSize;
		size -= packetSize;
	}
}

void AudioSender::ResetAudioStream(const std::string& error)
{
	LOG(LL_INFO, LM_SENDER, "resetting audio stream in 2sec, reason=%s", error.c_str());
	mEventLoop.Post(2s, [this]()
	{
		LOG(LL_INFO, LM_SENDER, "resetting audio stream now");
		soundio_instream_destroy(mInStream);
		soundio_device_unref(mInputDevice);
		soundio_destroy(mSoundIo);
		mSoundIo = CreateSoundIo(DEFAULT_SOUNDIO_BACKEND);
		StartAudioRecorder();
	});
}

}
