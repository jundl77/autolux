#pragma once

#include <cstddef>
#include <vector>

namespace DmxEnttecNode {

class AudioDelay
{
public:
	void Reset(double seconds, int sampleRate, int bytesPerFrame);
	void Process(char* data, size_t size);
	size_t Frames() const { return mSamples.size() / mBytesPerFrame; }

private:
	std::vector<char> mSamples;
	size_t mPosition {0};
	size_t mBytesPerFrame {1};
};

}
