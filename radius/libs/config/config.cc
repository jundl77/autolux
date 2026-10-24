#include "config.h"
#include <core/njson.h>
#include <core/logger.h>
#include <cmath>
#include <fstream>
#include <stdexcept>

namespace Radius {
namespace {

int Integer(const njson& j, const char* key, int fallback, int low, int high)
{
	if (!j.contains(key))
		return fallback;
	const auto& v = j.at(key);
	if (!v.is_number_integer())
		throw std::runtime_error(std::string(key) + " must be an integer");
	if (v.is_number_unsigned() && v.get<uint64_t>() > static_cast<uint64_t>(high))
		throw std::runtime_error(std::string(key) + " is out of range");
	const auto n = v.get<int64_t>();
	if (n < low || n > high)
		throw std::runtime_error(std::string(key) + " is out of range");
	return static_cast<int>(n);
}

} // namespace

std::optional<Config> Config::FromFile(const std::string& file, AppType type)
{
	std::ifstream input(file);
	if (!input)
		return std::nullopt;
	const auto j = njson::parse(input);
	Config c;
	const std::string level = j.value("log_level", "info");
	if (level == "debug")
		c.mLogLevel = LL_DEBUG;
	else if (level == "info")
		c.mLogLevel = LL_INFO;
	else if (level == "warn")
		c.mLogLevel = LL_WARN;
	else if (level == "error")
		c.mLogLevel = LL_ERROR;
	else
		throw std::runtime_error("invalid log_level");
	c.mCoreAffinity = Integer(j, "network_core", -1, -1, 31);
	c.mAudioCore = Integer(j, "audio_core", -1, -1, 31);
	c.mDmxCore = Integer(j, "dmx_core", -1, -1, 31);
	c.mOverlayListenPort = Integer(j, "overlay_port", 19001, 1, 65535);
	c.mDmxUniverse = Integer(j, "dmx_universe", 0, 0, 255);
	c.mBusPort = Integer(j, "bus_port", 19002, 1, 65535);
	c.mAudioSampleRate = Integer(j, "sample_rate", 44100, 44100, 44100);
	c.mJitterBufferMs = Integer(j, "jitter_buffer_ms", 150, 20, 150);
	c.mPeerAddress = j.at("peer_address").get<std::string>();
	c.mBindAddress = j.value("bind_address", "0.0.0.0");
	c.mArtnetAddress = j.value("artnet_address", "127.0.0.1");
	c.mAudioDeviceId = j.value("audio_device_id", "");
	c.mAudioEnabled = j.value("audio_enabled", true);
	c.mDmxEnabled = j.value("dmx_enabled", true);
	c.mArtnetEnabled = j.value("artnet_enabled", true);
	c.mPlaybackDelaySeconds = j.value("playback_delay_seconds", 14.0);
	if (!std::isfinite(c.mPlaybackDelaySeconds) || c.mPlaybackDelaySeconds < 0 || c.mPlaybackDelaySeconds > 120)
		throw std::runtime_error("playback_delay_seconds must be 0..120");
	if (j.contains("session"))
	{
		const auto& session = j.at("session");
		if (!session.is_number_unsigned() && (!session.is_number_integer() || session.get<int64_t>() <= 0))
			throw std::runtime_error("session must be a positive 64-bit integer");
		c.mSession = session.get<uint64_t>();
	}
	if (!c.mSession)
		throw std::runtime_error("session must be a positive 64-bit integer");
	return c;
}

} // namespace Radius
