#include "audio_playback.h"
#include "audio_utils.h"
#include <core/thread_config.h>
#include <cstring>
#include <stdexcept>

namespace Radius {

constexpr double PlaybackLatencySeconds = 0.02;

AudioPlayback::~AudioPlayback()
{
	if (mStream)
		soundio_outstream_destroy(mStream);
	if (mDevice)
		soundio_device_unref(mDevice);
	if (mIo)
		soundio_destroy(mIo);
}

void AudioPlayback::Start()
{
	mIo = CreateSoundIo(DEFAULT_SOUNDIO_BACKEND);
	mDevice = GetAudioOutputDevice(mIo, mConfig.mAudioDeviceId, false);
	mStream = soundio_outstream_create(mDevice);
	if (!mStream)
		throw std::runtime_error("unable to create playback stream");
	mStream->userdata = this;
	mStream->format = SoundIoFormatFloat32LE;
	mStream->layout = *soundio_channel_layout_get_builtin(SoundIoChannelLayoutIdStereo);
	mStream->sample_rate = mConfig.mAudioSampleRate;
	mStream->software_latency = PlaybackLatencySeconds;
	mStream->write_callback = Write;
	mStream->underflow_callback = [](SoundIoOutStream* s) {
		++static_cast<AudioPlayback*>(s->userdata)->deviceUnderruns;
	};
	mStream->error_callback = [](SoundIoOutStream* s, int e) {
		static_cast<AudioPlayback*>(s->userdata)->error = e;
	};
	const int open = soundio_outstream_open(mStream);
	if (open)
		throw std::runtime_error(std::string("playback open: ") + soundio_strerror(open));
	if (mStream->bytes_per_frame != Bus::AudioBytesPerFrame || mStream->layout.channel_count != Bus::Channels)
		throw std::runtime_error("playback requires stereo Float32LE");
	const int start = soundio_outstream_start(mStream);
	if (start)
		throw std::runtime_error(std::string("playback start: ") + soundio_strerror(start));
}

void AudioPlayback::Write(SoundIoOutStream* stream, int minimum, int maximum) noexcept
{
	auto& self = *static_cast<AudioPlayback*>(stream->userdata);
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
	self.playout.BeginCallback();
	int remaining = maximum;
	while (remaining > 0)
	{
		SoundIoChannelArea* areas = nullptr;
		int count = remaining;
		const int begin = soundio_outstream_begin_write(stream, &areas, &count);
		if (begin)
		{
			self.error = begin;
			return;
		}
		if (count <= 0)
			break;
		for (int frame = 0; frame < count; ++frame)
		{
			float stereo[Bus::Channels];
			self.playout.Frame(stereo);
			for (size_t ch = 0; ch < Bus::Channels; ++ch)
				std::memcpy(areas[ch].ptr + frame * areas[ch].step, &stereo[ch], sizeof(float));
		}
		const int end = soundio_outstream_end_write(stream);
		if (end)
		{
			self.error = end;
			return;
		}
		remaining -= count;
	}
	double latency = 0;
	if (soundio_outstream_get_latency(stream, &latency) == 0)
	{
		const auto pending = static_cast<uint64_t>(latency * stream->sample_rate);
		const auto played = self.playout.played.load();
		self.audibleWatermark = played > pending ? played - pending : 0;
	}
	else
		self.audibleWatermark = self.playout.played.load();
}

} // namespace Radius
