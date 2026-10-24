#include "radius_rx.h"
#include <core/thread_config.h>
#include <enttec/usb_client.h>
#include <future>

namespace Radius {

constexpr uint64_t ReceiverInputTimeoutMilliseconds = 1000;
constexpr auto DmxSchedulePollInterval = 5ms;

RadiusRx::~RadiusRx()
{
	mDmxLoop.Stop();
	if (mDmxThread.joinable())
		mDmxThread.join();
}

void RadiusRx::Start()
{
	ConfigureThread(mConfig.mCoreAffinity);
	mBus.Open(mConfig.mBindAddress, mConfig.mPeerAddress, mConfig.mBusPort, true);
	if (mConfig.mDmxEnabled)
		StartDmx();
	if (mConfig.mAudioEnabled)
		mAudio.Start();
	mPoll = mLoop.AddPoller([this] {
		Tick();
	});
}

void RadiusRx::StartDmx()
{
	std::promise<void> ready;
	auto started = ready.get_future();
	mDmxThread = std::thread([this, ready = std::move(ready)]() mutable {
		bool signalled = false;
		try
		{
			ConfigureThread(mConfig.mDmxCore);
			EnttecUSBClient usb(mDmxLoop);
			usb.Start();
			mDmxConnected = true;
			bool dark = true;
			auto timer = mDmxLoop.AddTimer(DmxSchedulePollInterval, [&] {
				// Read published input times first; a concurrent update must not be newer than now.
				const auto dmxAt = mDmxAt.load();
				const auto audioAt = mAudioAt.load();
				const auto now = SteadyMs();
				const bool stale =
				    IsInputStale(now, dmxAt, ReceiverInputTimeoutMilliseconds) ||
				    (mConfig.mAudioEnabled && IsInputStale(now, audioAt, ReceiverInputTimeoutMilliseconds));
				if (stale)
				{
					if (!dark)
					{
						std::array<uint8_t, DmxFrameSize> zero{};
						usb.UpdateFrame(DmxFrame(zero));
						dark = true;
					}
					while (mDmxQueue.Front())
						mDmxQueue.Pop();
					return;
				}
				const auto watermark = mAudio.audibleWatermark.load();
				std::array<uint8_t, DmxFrameSize> frame{};
				bool changed = false;
				while (const auto* p = mDmxQueue.Front())
				{
					if (mConfig.mAudioEnabled && p->watermark > watermark)
						break;
					frame = p->channels;
					mDmxLateness = watermark > p->watermark ? watermark - p->watermark : 0;
					mDmxQueue.Pop();
					changed = true;
				}
				if (changed)
				{
					usb.UpdateFrame(DmxFrame(frame));
					++mDmxFrames;
					dark = false;
				}
			});
			ready.set_value();
			signalled = true;
			mDmxLoop.Run(RunHot::No);
		}
		catch (...)
		{
			mDmxFailed = true;
			if (!signalled)
				ready.set_exception(std::current_exception());
		}
		mDmxConnected = false;
	});
	started.get();
}

void RadiusRx::Tick()
{
	const auto now = SteadyMs();
	if (mAudio.error)
		throw std::runtime_error("audio playback failed: " + std::to_string(mAudio.error.load()));
	if (mDmxFailed)
		throw std::runtime_error("DMX output worker failed");
	mBus.Poll(mConfig.mSession, [&](const Bus::Envelope& p) {
		if (p.has_heartbeat())
		{
			mLink.Received(now);
			++mHeartbeatsReceived;
		}
		else if (p.has_audio() && mConfig.mAudioEnabled)
		{
			Bus::AudioBlock block;
			block.watermark = p.audio_frame_position();
			block.frames = static_cast<uint16_t>(p.audio().interleaved_samples_size() / Bus::Channels);
			std::copy(p.audio().interleaved_samples().begin(), p.audio().interleaved_samples().end(),
			          block.samples.begin());
			for (size_t i = 0; i < block.frames * Bus::Channels; ++i)
				mAudioPeak = (std::max)(mAudioPeak, std::abs(block.samples[i]));
			if (mAudio.playout.Push(block))
			{
				const auto previous = mAudioAt.load();
				if (previous)
					mAudioMaxGap = (std::max)(mAudioMaxGap, now - previous);
				mAudioAt = now;
			}
		}
		else if (p.has_lighting() && mConfig.mDmxEnabled)
		{
			if (!Bus::IsNewerSequenceNumber(p.sequence_number(), mLastDmxSequence))
			{
				++mDmxLate;
				return;
			}
			mLastDmxSequence = p.sequence_number();
			Bus::LightingFrame frame;
			frame.watermark = p.audio_frame_position();
			std::memcpy(frame.channels.data(), p.lighting().channels().data(), DmxFrameSize);
			if (!mDmxQueue.Push(frame))
				++mDmxDrops;
			mDmxAt = now;
		}
	});
	if (mLink.Expired(now))
		throw std::runtime_error("transmitter heartbeat timed out after 30 seconds");
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
	if (now - mHealthAt >= Bus::HealthReportIntervalMilliseconds)
	{
		const auto& audio = mAudio.playout;
		auto p = Bus::NewEnvelope(mConfig.mSession);
		auto& health = *p.mutable_receiver_telemetry();
		health.set_up(true);
		health.set_link_up(mLink.Up(now));
		health.set_heartbeat_age_seconds(mLink.Age(now));
		health.set_heartbeats_sent(mHeartbeatsSent);
		health.set_heartbeats_received(mHeartbeatsReceived);
		health.set_audio_buffer_seconds(audio.bufferedFrames.load() / double(mConfig.mAudioSampleRate));
		health.set_buffer_target_seconds(mConfig.mJitterBufferMs / double(MillisecondsPerSecond));
		health.set_underruns_total(audio.underruns.load());
		health.set_device_underruns_total(mAudio.deviceUnderruns.load());
		health.set_concealed_frames_total(audio.concealedFrames.load());
		health.set_lost_frames_total(audio.lostFrames.load());
		health.set_overflow_frames_total(audio.overflowFrames.load());
		health.set_late_packets_total(audio.latePackets.load());
		health.set_latency_trims_total(audio.latencyTrims.load());
		health.set_clock_correction_ppm(audio.driftPpm.load());
		health.set_dmx_connected(mDmxConnected.load());
		health.set_dmx_frames_total(mDmxFrames.load());
		health.set_dmx_dropped_total(mDmxDrops + mDmxLate);
		health.set_dmx_lateness_seconds(mDmxLateness.load() / double(mConfig.mAudioSampleRate));
		const auto audioAt = mAudioAt.load();
		if (audioAt)
		{
			health.set_audio_input_age_seconds((now - audioAt) / double(MillisecondsPerSecond));
			health.set_audio_max_gap_seconds((std::max)(mAudioMaxGap, now - audioAt) / double(MillisecondsPerSecond));
		}
		health.set_audio_signal_peak(mAudioPeak);
		health.set_invalid_packets_total(mBus.invalidPackets);
		health.set_send_failures_total(mBus.sendFailures);
		mBus.Send(p);
		WriteHealth({{"session", mConfig.mSession}, {"rx", Bus::ReceiverTelemetryToJson(health)}});
		mHealthAt = now;
		mAudioMaxGap = 0;
		mAudioPeak = 0;
	}
}

} // namespace Radius
