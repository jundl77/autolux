#include "audio_capture.h"
#include "audio_utils.h"
#include <core/thread_config.h>
#include <stdexcept>

namespace Radius {

constexpr double CaptureLatencySeconds = 0.01;

AudioCapture::~AudioCapture()
{
	if (mStream)
		soundio_instream_destroy(mStream);
	if (mDevice)
		soundio_device_unref(mDevice);
	if (mIo)
		soundio_destroy(mIo);
}

void AudioCapture::Start()
{
	mIo = CreateSoundIo(DEFAULT_SOUNDIO_BACKEND);
	mDevice = GetAudioInputDevice(mIo, mConfig.mAudioDeviceId, false);
	mStream = soundio_instream_create(mDevice);
	if (!mStream)
		throw std::runtime_error("unable to create capture stream");
	mStream->userdata = this;
	mStream->format = SoundIoFormatFloat32LE;
	mStream->layout = *soundio_channel_layout_get_builtin(SoundIoChannelLayoutIdStereo);
	mStream->sample_rate = mConfig.mAudioSampleRate;
	mStream->software_latency = CaptureLatencySeconds;
	mStream->read_callback = Read;
	mStream->overflow_callback = [](SoundIoInStream* s) {
		++static_cast<AudioCapture*>(s->userdata)->captureOverruns;
	};
	mStream->error_callback = [](SoundIoInStream* s, int e) {
		static_cast<AudioCapture*>(s->userdata)->error = e;
	};
	const int open = soundio_instream_open(mStream);
	if (open)
		throw std::runtime_error(std::string("capture open: ") + soundio_strerror(open));
	if (mStream->bytes_per_frame != Bus::AudioBytesPerFrame || mStream->layout.channel_count != Bus::Channels)
		throw std::runtime_error("capture requires stereo Float32LE");
	mDelay.Reset(mConfig.mPlaybackDelaySeconds, mConfig.mAudioSampleRate, Bus::AudioBytesPerFrame);
	const int start = soundio_instream_start(mStream);
	if (start)
		throw std::runtime_error(std::string("capture start: ") + soundio_strerror(start));
}

void AudioCapture::Read(SoundIoInStream* stream, int, int maximum) noexcept
{
	auto& self = *static_cast<AudioCapture*>(stream->userdata);
	if (!self.mThreadConfigured)
	{
		try
		{
			ConfigureThread(self.mConfig.mAudioCore);
			self.mThreadConfigured = true;
		}
		catch (...)
		{
			self.error = SoundIoErrorSystemResources;
			return;
		}
	}
	int remaining = maximum;
	while (remaining > 0)
	{
		SoundIoChannelArea* areas = nullptr;
		int count = remaining;
		int result = soundio_instream_begin_read(stream, &areas, &count);
		if (result)
		{
			self.error = result;
			return;
		}
		if (count == 0)
			return;
		if (count < 0 || count > remaining)
		{
			self.error = SoundIoErrorInvalid;
			soundio_instream_end_read(stream);
			return;
		}
		if (!areas)
			++self.captureOverruns;
		for (int offset = 0; offset < count;)
		{
			const int chunk = (std::min)(count - offset, static_cast<int>(Bus::AudioFrames));
			Bus::AudioBlock block;
			block.frames = static_cast<uint16_t>(chunk);
			block.watermark = self.mFrame;
			if (areas)
				for (int frame = 0; frame < chunk; ++frame)
					for (size_t ch = 0; ch < Bus::Channels; ++ch)
						std::memcpy(&block.samples[Bus::Channels * frame + ch],
						            areas[ch].ptr + (offset + frame) * areas[ch].step, sizeof(float));
			self.mDelay.Process(reinterpret_cast<char*>(block.samples.data()), chunk * Bus::AudioBytesPerFrame);
			if (!self.mQueue.Push(block))
				self.droppedFrames += chunk;
			self.mFrame += chunk;
			self.watermark = self.mFrame;
			offset += chunk;
		}
		result = soundio_instream_end_read(stream);
		if (result)
		{
			self.error = result;
			return;
		}
		remaining -= count;
	}
}

} // namespace Radius
