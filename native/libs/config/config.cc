#include "config.h"
#include <core/logger.h>
#include <core/njson.h>
#include <core/throw_if.h>
#include <fstream>
#include <cmath>

namespace DmxEnttecNode {

namespace {
const LogModule LM_CONFIG {"CONFIG"};

int ParseLogLevel(const std::string& logLevel)
{
	if (logLevel == "debug")
		return LL_DEBUG;
	if (logLevel == "info")
		return LL_INFO;
	if (logLevel == "warn")
		return LL_WARN;
	if (logLevel == "error")
		return LL_ERROR;

	throw std::runtime_error("unknown log-level '" + logLevel + "', options are: debug|info|warn|error");
}

RunHot ParseRunHot(bool runHot)
{
	if (runHot)
		return RunHot::Yes;
	return RunHot::No;
}

void LoadCommonAudioConfig(Config& config, njson& json)
{
	config.mAudioServerPort = json["audio_server_port"];
	LOG(LL_INFO, LM_CONFIG, "audio_server_port: %d", config.mAudioServerPort);

	config.mAudioDeviceId = json["audio_device_id"];
	LOG(LL_INFO, LM_CONFIG, "audio_device_id: %s", config.mAudioDeviceId.c_str());

	config.mAudioFormat = json["audio_format"];
	LOG(LL_INFO, LM_CONFIG, "audio_format: %s", config.mAudioFormat.c_str());

	config.mAudioSampleRate = json["audio_sample_rate"];
	LOG(LL_INFO, LM_CONFIG, "audio_sample_rate: %d", config.mAudioSampleRate);
}

void LoadConfigAudioSender(Config& config, njson& json)
{
	LoadCommonAudioConfig(config, json);
	config.mPlaybackDelaySeconds = json.value("playback_delay_seconds", 0.0);
	THROW_IF(!std::isfinite(config.mPlaybackDelaySeconds) || config.mPlaybackDelaySeconds < 0 ||
			 config.mPlaybackDelaySeconds > 120, "playback_delay_seconds must be between 0 and 120");
	THROW_IF(config.mPlaybackDelaySeconds > 0 && config.mAudioFormat != "SoundIoFormatFloat32LE",
			 "delayed playback requires SoundIoFormatFloat32LE");
	LOG(LL_INFO, LM_CONFIG, "playback_delay_seconds: %.6f", config.mPlaybackDelaySeconds);

	config.mAudioServerIpv4 = json["audio_server_ip"];
	LOG(LL_INFO, LM_CONFIG, "audio_server_ip: %s", config.mAudioServerIpv4.c_str());
}

void LoadConfigAudioReceiver(Config& config, njson& json)
{
	LoadCommonAudioConfig(config, json);
}

void LoadConfigDmxUsbNode(Config& config, njson& json)
{
	config.mOverlayListenPort = json["overlay_listen_port"];
	LOG(LL_INFO, LM_CONFIG, "overlay_listen_port: %d", config.mOverlayListenPort);

	config.mDmxUniverse = json["dmx_universe"];
	THROW_IF(config.mDmxUniverse < 0, "dmx universe cannot be negative");
	LOG(LL_INFO, LM_CONFIG, "dmx_universe: %d", config.mDmxUniverse);
}

}

std::optional<Config> Config::FromFile(const std::string& filePath, AppType appType)
{
	LOG(LL_INFO, LM_CONFIG, "loading config from file: %s", filePath.c_str());

	std::ifstream ifs(filePath);
	if (!ifs.good())
	{
		LOG(LL_ERROR, LM_CONFIG, "unable to find config file: %s", filePath.c_str());
		return std::nullopt;
	}

	njson json = njson::parse(ifs);
	Config config {};

	LOG(LL_INFO, LM_CONFIG, "loading config..");

	std::string logLevel = json["log_level"];
	config.mLogLevel = ParseLogLevel(logLevel);
	LOG(LL_INFO, LM_CONFIG, "log_level: %s", LogLevelToString(config.mLogLevel).c_str());

	config.mRunHot = ParseRunHot(json["run_hot"]);
	LOG(LL_INFO, LM_CONFIG, "run_hot: %d", config.mRunHot);

	config.mCoreAffinity = json["core_affinity"];
	LOG(LL_INFO, LM_CONFIG, "core_affinity: %d", config.mCoreAffinity);

	switch (appType) {
		case AppType::AudioSender:
			LoadConfigAudioSender(config, json);
			break;
		case AppType::AudioReceiver:
			LoadConfigAudioReceiver(config, json);
			break;
		case AppType::DmxUsbNode:
			LoadConfigDmxUsbNode(config, json);
			break;
	}

	LOG(LL_INFO, LM_CONFIG, "done loading config.");

	return config;
}

}
