#include "radius_tx.h"
#include <core/thread_config.h>

namespace Radius {

constexpr int MaxAudioPacketsPerTick = 128;
constexpr uint64_t DmxSendIntervalMilliseconds = 25;
constexpr uint64_t DmxInputTimeoutMilliseconds = 3000;

void RadiusTx::Start()
{
	ConfigureThread(mConfig.mCoreAffinity);
	mBus.Open(mConfig.mBindAddress, mConfig.mPeerAddress, mConfig.mBusPort, false);
	if (mConfig.mArtnetEnabled)
	{
		mArtnet.StartListening();
		mOverlay.Start();
	}
	if (mConfig.mAudioEnabled)
		mAudio.Start();
	mPoll = mLoop.AddPoller([this] {
		Tick();
	});
}

void RadiusTx::Tick()
{
	const auto now = SteadyMs();
	if (mAudio.error)
		throw std::runtime_error("audio capture failed: " + std::to_string(mAudio.error.load()));
	mBus.Poll(mConfig.mSession, [&](const Bus::Envelope& p) {
		if (p.has_heartbeat())
		{
			mLink.Received(now);
			++mHeartbeatsReceived;
		}
		if (p.has_receiver_telemetry())
		{
			mReceiver = Bus::ReceiverTelemetryToJson(p.receiver_telemetry());
			mReceiverAt = now;
		}
	});
	if (mLink.Expired(now))
		throw std::runtime_error("receiver heartbeat timed out after 30 seconds");
	if (mLink.Due(now))
	{
		auto p = Bus::NewEnvelope(mConfig.mSession);
		p.mutable_heartbeat();
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
		auto p = Bus::NewEnvelope(mConfig.mSession);
		p.set_audio_frame_position(block->watermark);
		p.set_sequence_number(++mAudioSequence);
		auto* audio = p.mutable_audio();
		audio->set_sample_rate_hz(mConfig.mAudioSampleRate);
		audio->set_channel_count(Bus::Channels);
		audio->mutable_interleaved_samples()->Add(block->samples.begin(),
		                                          block->samples.begin() + block->frames * Bus::Channels);
		mBus.Send(p);
		mAudio.Pop();
	}
	if (mConfig.mArtnetEnabled && now - mLastDmxSend >= DmxSendIntervalMilliseconds)
	{
		auto p = Bus::NewEnvelope(mConfig.mSession);
		p.set_audio_frame_position(mAudio.watermark.load());
		p.set_sequence_number(++mDmxSequence);
		std::array<uint8_t, DmxFrameSize> frame{};
		if (mDmxAt && now - mDmxAt < DmxInputTimeoutMilliseconds)
			frame = mDmx.Compose();
		p.mutable_lighting()->set_channels(frame.data(), frame.size());
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
		WriteHealth({{"session", mConfig.mSession},
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
	mDmx.Base(frame);
	mDmxAt = SteadyMs();
}

void RadiusTx::OnOverlayMessage(const OverlayIdl::OverlayMessage& overlay)
{
	if (overlay.mUniverse == mConfig.mDmxUniverse)
		mDmx.Overlay(overlay);
}

} // namespace Radius
