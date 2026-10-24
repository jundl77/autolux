#pragma once

#include <core/types.h>
#include <core/event_loop.h>

#include <cstdint>
#include <iostream>
#include <chrono>
#include <unordered_map>
#include <string>
#include <optional>

namespace DmxEnttecNode {

enum class AppType
{
	AudioSender,
	AudioReceiver,
	DmxUsbNode
};

class Config
{
public:
	static std::optional<Config> FromFile(const std::string& filename, AppType);

	int mLogLevel;
	RunHot mRunHot { RunHot::No };
	int mCoreAffinity; // core number on which to run, only takes effect if mRunHot is RunHot::Yes
	std::string mAppName;

	uint32_t mOverlayListenPort;
	uint16_t mDmxUniverse;

	std::string mAudioServerIpv4;
	uint32_t mAudioServerPort;
	std::string mAudioDeviceId;
	std::string mAudioFormat;
	int mAudioSampleRate;
	double mPlaybackDelaySeconds {0.0};
};

}
