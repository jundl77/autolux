#include "radius_tx.h"
#include <core/thread_config.h>
#include <soundio/audio_utils.h>
#include <algorithm>

namespace Radius {

constexpr int MaxAudioPacketsPerTick = 128;
constexpr uint64_t DmxSendIntervalMilliseconds = 25;
constexpr uint64_t DmxInputTimeoutMilliseconds = 3000;

void RadiusTx::Start()
{
	ConfigureThread(mConfig.mCoreAffinity);
	mBus.Open(mConfig.mBindAddress, mConfig.mPeerAddress, mConfig.mBusPort, false);
	mLogin.set_command_id(Bus::LoginCommandId);
	mLogin.set_run_id(mConfig.mRunId);
	mLogin.set_protocol_major(Bus::PROTOCOL_MAJOR);
	mLogin.set_protocol_minor(Bus::PROTOCOL_MINOR);
	mLogin.set_sample_rate_hz(mConfig.mAudioSampleRate);
	mLogin.set_channel_count(Bus::Channels);
	mLogin.set_audio_enabled(mConfig.mReceiverAudioEnabled);
	mLogin.set_lighting_enabled(mConfig.mReceiverLightingEnabled);
	mLogin.set_jitter_buffer_ms(mConfig.mReceiverJitterBufferMs);
	mLoginStartedAt = SteadyMs();
	mPoll = mLoop.AddPoller([this] {
		Tick();
	});
}

void RadiusTx::Tick()
{
	auto now = SteadyMs();
	if (mAudio.error)
		throw std::runtime_error(AudioStreamError("audio capture", mAudio.error.load()));
	mBus.Poll(mConfig.mRunId, [&](const Bus::Message& p) {
		if (const auto* reply = std::get_if<Bus::LoginReply>(&p))
		{
			if (reply->command_id() != mLogin.command_id() || reply->protocol_major() != Bus::PROTOCOL_MAJOR ||
			    reply->protocol_minor() > Bus::PROTOCOL_MINOR || reply->max_message_bytes() != Bus::MaxDatagramBytes)
				throw std::runtime_error("receiver returned an incompatible LoginReply");
			if (!mReady)
			{
				if (mConfig.mArtnetEnabled)
					mArtnet.StartListening();
				if (mConfig.mAudioEnabled)
					mAudio.Start();
				mReady = true;
				mLink = LinkState(SteadyMs());
				mLink.Received(SteadyMs());
			}
			return;
		}
		if (const auto* error = std::get_if<Bus::ErrorReply>(&p))
		{
			if (error->command_id() == mLogin.command_id())
				throw std::runtime_error("receiver rejected Login: " + error->message());
			++mBus.invalidPackets;
			return;
		}
		if (!mReady)
		{
			++mBus.invalidPackets;
			return;
		}
		if (std::holds_alternative<Bus::Heartbeat>(p))
		{
			mLink.Received(now);
			++mHeartbeatsReceived;
		}
		if (const auto* metrics = std::get_if<Bus::Metrics>(&p))
		{
			mReceiver = Bus::MetricsToJson(*metrics);
			mReceiverAt = now;
		}
	});
	now = SteadyMs();
	if (!mReady)
	{
		if (now - mLoginStartedAt >= Bus::LoginTimeoutMilliseconds)
			throw std::runtime_error("receiver Login timed out");
		if (!mLoginSentAt || now - mLoginSentAt >= Bus::LoginRetryMilliseconds)
		{
			mBus.Send(mLogin);
			mLoginSentAt = now;
		}
		return;
	}
	if (mLink.Expired(now))
		throw std::runtime_error("receiver heartbeat timed out after 30 seconds");
	if (mLink.Due(now))
	{
		Bus::Heartbeat p;
		p.set_run_id(mConfig.mRunId);
		if (mBus.Send(p))
		{
			mLink.Sent(now);
			++mHeartbeatsSent;
		}
	}
	for (int i = 0; i < MaxAudioPacketsPerTick; ++i)
	{
		const auto* block = mAudio.Front();
		if (!block)
			break;
		Bus::AudioFrame p;
		p.set_run_id(mConfig.mRunId);
		p.set_audio_frame_position(block->watermark);
		p.set_sequence_number(++mAudioSequence);
		p.mutable_interleaved_samples()->Add(block->samples.begin(),
		                                     block->samples.begin() + block->frames * Bus::Channels);
		mBus.Send(p);
		mAudio.Pop();
	}
	if (mConfig.mArtnetEnabled && now - mLastDmxSend >= DmxSendIntervalMilliseconds)
	{
		Bus::LightFrame p;
		p.set_run_id(mConfig.mRunId);
		p.set_audio_frame_position(mAudio.watermark.load());
		p.set_sequence_number(++mDmxSequence);
		std::array<uint8_t, DmxFrameSize> frame{};
		if (mDmxAt && now - mDmxAt < DmxInputTimeoutMilliseconds)
			frame = mDmx;
		p.set_channels(frame.data(), frame.size());
		mBus.Send(p);
		mLastDmxSend = now;
	}
	if (now - mHealthAt >= Bus::HealthReportIntervalMilliseconds)
	{
		njson tx = {
		    {"up", true},
		    {"link_up", mLink.Up(now)},
		    {"heartbeat_age_seconds", mLink.Age(now)},
		    {"heartbeats_sent", mHeartbeatsSent},
		    {"heartbeats_received", mHeartbeatsReceived},
		    {"audio_buffer_seconds", mAudio.QueuedFrames() / double(mConfig.mAudioSampleRate)},
		    {"capture_overruns_total", mAudio.captureOverruns.load()},
		    {"queue_dropped_frames_total", mAudio.droppedFrames.load()},
		    {"send_failures_total", mBus.sendFailures},
		    {"invalid_packets_total", mBus.invalidPackets},
		    {"playback_delay_seconds", mConfig.mPlaybackDelaySeconds},
		    {"dmx_input_age_seconds", mDmxAt ? njson((now - mDmxAt) / double(MillisecondsPerSecond)) : njson(nullptr)}};
		WriteHealth({{"run_id", mConfig.mRunId},
		             {"tx", tx},
		             {"rx", mReceiver},
		             {"rx_age_seconds",
		              mReceiverAt ? njson((now - mReceiverAt) / double(MillisecondsPerSecond)) : njson(nullptr)}});
		mHealthAt = now;
	}
}

void RadiusTx::OnDmxMessage(const DmxFrame& frame, int universe)
{
	if (universe != mConfig.mDmxUniverse)
		return;
	std::copy(frame.begin(), frame.end(), mDmx.begin());
	mDmxAt = SteadyMs();
}

} // namespace Radius
