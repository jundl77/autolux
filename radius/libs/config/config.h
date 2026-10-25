#pragma once
#include <core/event_loop.h>
#include <optional>
#include <string>
#include <cstdint>

namespace Radius {

enum class AppType
{
	Tx,
	Rx
};

struct Config
{
	static std::optional<Config> FromFile(const std::string&, AppType);
	int mLogLevel = 1;
	RunHot mRunHot = RunHot::No;
	int mCoreAffinity = -1, mAudioCore = -1, mDmxCore = -1;
	std::string mAppName;
	uint16_t mDmxUniverse = 0;
	std::string mPeerAddress, mBindAddress = "0.0.0.0", mArtnetAddress;
	uint16_t mBusPort = 19002;
	uint64_t mSession = 0;
	std::string mAudioDeviceId;
	int mAudioSampleRate = 44100, mJitterBufferMs = 150;
	double mPlaybackDelaySeconds = 14;
	bool mAudioEnabled = true, mDmxEnabled = true, mArtnetEnabled = true;
	int mReceiverJitterBufferMs = 150;
	bool mReceiverAudioEnabled = true, mReceiverLightingEnabled = true;
};

} // namespace Radius
