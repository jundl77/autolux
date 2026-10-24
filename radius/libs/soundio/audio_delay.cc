#include "audio_delay.h"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace Radius {

void AudioDelay::Reset(double seconds, int sampleRate, int bytesPerFrame)
{
	if (!std::isfinite(seconds) || seconds < 0 || seconds > 120 || sampleRate <= 0 || bytesPerFrame <= 0)
		throw std::invalid_argument("invalid audio delay or frame geometry (delay must be 0..120 seconds)");

	mBytesPerFrame = bytesPerFrame;
	mDelayBuffer.assign(static_cast<size_t>(std::llround(seconds * sampleRate)) * mBytesPerFrame, 0);
	mBufferOffsetBytes = 0;
}

void AudioDelay::Process(char* data, size_t size)
{
	if (size % mBytesPerFrame != 0)
		throw std::invalid_argument("audio delay requires complete frames");
	if (mDelayBuffer.empty())
		return;

	while (size > 0)
	{
		const size_t count = (std::min)(size, mDelayBuffer.size() - mBufferOffsetBytes);
		std::swap_ranges(data, data + count, mDelayBuffer.data() + mBufferOffsetBytes);
		mBufferOffsetBytes = (mBufferOffsetBytes + count) % mDelayBuffer.size();
		data += count;
		size -= count;
	}
}

} // namespace Radius
