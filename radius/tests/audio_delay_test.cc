#include "soundio/audio_delay.h"

#include <algorithm>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <vector>

using Radius::AudioDelay;

void Check(bool value)
{
	if (!value)
		throw std::runtime_error("audio delay test failed");
}

void CheckStream(double seconds, int rate, const std::vector<size_t>& chunks)
{
	AudioDelay delay;
	delay.Reset(seconds, rate, 2 * sizeof(float));
	const size_t lag = static_cast<size_t>(std::llround(seconds * rate));
	Check(delay.Frames() == lag);
	size_t position = 0;
	for (size_t frames : chunks)
	{
		std::vector<float> audio(frames * 2);
		for (size_t i = 0; i < audio.size(); ++i)
			audio[i] = static_cast<float>(position * 2 + i + 1);
		delay.Process(reinterpret_cast<char*>(audio.data()), audio.size() * sizeof(float));
		for (size_t i = 0; i < frames; ++i)
			for (size_t channel = 0; channel < 2; ++channel)
				Check(audio[i * 2 + channel] == (position + i < lag ? 0.0f :
					static_cast<float>((position + i - lag) * 2 + channel + 1)));
		position += frames;
	}
}

int main()
{
	CheckStream(0, 44100, {1, 256, 0, 5000});
	CheckStream(0.25, 100, {3, 17, 9, 110, 1, 30});
	CheckStream(14, 44100, std::vector<size_t>(3000, 256));
	CheckStream(14, 48000, std::vector<size_t>(1600, 512));
	AudioDelay delay;
	delay.Reset(1, 1, sizeof(float));
	float sample = 1;
	delay.Process(reinterpret_cast<char*>(&sample), sizeof(sample));
	delay.Reset(1, 1, sizeof(float));
	sample = 2;
	delay.Process(reinterpret_cast<char*>(&sample), sizeof(sample));
	Check(sample == 0);
	for (double invalid : {-1.0, 121.0, static_cast<double>(INFINITY), static_cast<double>(NAN)})
	{
		bool rejected = false;
		try { delay.Reset(invalid, 44100, 8); }
		catch (const std::invalid_argument&) { rejected = true; }
		Check(rejected);
	}
	std::cout << "audio delay: frame-exact stereo delay, wrap, reset and validation passed\n";
}
