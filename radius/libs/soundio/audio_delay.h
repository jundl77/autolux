#pragma once

#include <cstddef>
#include <vector>

namespace Radius {

class AudioDelay
{
public:
	void Reset(double seconds, int sampleRate, int bytesPerFrame);
	void Process(char* data, size_t size);

	size_t Frames() const
	{
		return mDelayBuffer.size() / mBytesPerFrame;
	}

private:
	std::vector<char> mDelayBuffer;
	size_t mBufferOffsetBytes{0};
	size_t mBytesPerFrame{1};
};

} // namespace Radius
