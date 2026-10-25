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
	mPoll = mLoop.AddPoller([this] {
		Tick();
	});
}

void RadiusRx::Login(const Bus::Login& login)
{
	mLastProcessedCommandId = (std::max)(mLastProcessedCommandId, login.command_id());
	const auto reject = [&](std::string reason) {
		Bus::ErrorReply reply;
		reply.set_run_id(mConfig.mRunId);
		reply.set_command_id(login.command_id());
		reply.set_message(reason.substr(0, Bus::MaxErrorBytes));
		mBus.Send(reply);
		if (!mLogin)
			mBus.ReleasePeer();
	};
	if (mLogin && mLogin->SerializeAsString() != login.SerializeAsString())
	{
		reject("receiver run is already logged in");
		return;
	}
	if (!mLogin)
	{
		if (login.protocol_major() != Bus::PROTOCOL_MAJOR)
		{
			reject("incompatible protocol major version");
			return;
		}
		if (login.sample_rate_hz() != Bus::SampleRateHz || login.channel_count() != Bus::Channels)
		{
			reject("receiver requires 44100 Hz stereo audio");
			return;
		}
		if (login.jitter_buffer_ms() < Bus::MinJitterBufferMilliseconds ||
		    login.jitter_buffer_ms() > Bus::MaxJitterBufferMilliseconds)
		{
			reject("receiver jitter buffer must be 20..150 ms");
			return;
		}
		mConfig.mAudioEnabled = login.audio_enabled();
		mConfig.mDmxEnabled = login.lighting_enabled();
		mConfig.mJitterBufferMs = static_cast<int>(login.jitter_buffer_ms());
		try
		{
			mAudio = std::make_unique<AudioPlayback>(mConfig);
			if (mConfig.mAudioEnabled)
				mAudio->Start();
			if (mConfig.mDmxEnabled)
				StartDmx();
		}
		catch (const std::exception& error)
		{
			reject(std::string("receiver initialization failed: ") + error.what());
			throw; // The owning app releases partially initialized output resources.
		}
		mLogin = login;
		const auto now = SteadyMs();
		mLink = LinkState(now);
		mLink.Received(now);
	}
	Bus::LoginReply reply;
	reply.set_command_id(login.command_id());
	reply.set_run_id(mConfig.mRunId);
	reply.set_protocol_major(Bus::PROTOCOL_MAJOR);
	reply.set_protocol_minor((std::min<uint32_t>)(login.protocol_minor(), Bus::PROTOCOL_MINOR));
	reply.set_max_message_bytes(Bus::MaxDatagramBytes);
	mBus.Send(reply);
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
			mDmxFrames = usb.FramesWrittenTotal();
			mDmxConnected = true;
			bool dark = true;
			auto timer = mDmxLoop.AddTimer(DmxSchedulePollInterval, [&] {
				mDmxFrames = usb.FramesWrittenTotal();
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
				const auto watermark = mAudio->audibleWatermark.load();
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
	auto now = SteadyMs();
	if (mAudio && mAudio->error)
		throw std::runtime_error("audio playback failed: " + std::to_string(mAudio->error.load()));
	if (mDmxFailed)
		throw std::runtime_error("DMX output worker failed");
	mBus.Poll(mConfig.mRunId, [&](const Bus::Message& p) {
		if (const auto* login = std::get_if<Bus::Login>(&p))
		{
			Login(*login);
			return;
		}
		if (!mLogin)
		{
			++mBus.invalidPackets;
			return;
		}
		if (std::holds_alternative<Bus::Heartbeat>(p))
		{
			mLink.Received(now);
			++mHeartbeatsReceived;
		}
		else if (const auto* audio = std::get_if<Bus::AudioFrame>(&p); audio && mConfig.mAudioEnabled)
		{
			Bus::AudioBlock block;
			block.watermark = audio->audio_frame_position();
			block.frames = static_cast<uint16_t>(audio->interleaved_samples_size() / Bus::Channels);
			std::copy(audio->interleaved_samples().begin(), audio->interleaved_samples().end(), block.samples.begin());
			for (size_t i = 0; i < block.frames * Bus::Channels; ++i)
				mAudioPeak = (std::max)(mAudioPeak, std::abs(block.samples[i]));
			if (mAudio->playout.Push(block))
			{
				const auto previous = mAudioAt.load();
				if (previous)
					mAudioMaxGap = (std::max)(mAudioMaxGap, now - previous);
				mAudioAt = now;
			}
		}
		else if (const auto* lighting = std::get_if<Bus::LightFrame>(&p); lighting && mConfig.mDmxEnabled)
		{
			if (!Bus::IsNewerSequenceNumber(lighting->sequence_number(), mLastDmxSequenceNumber))
			{
				++mDmxLate;
				return;
			}
			mLastDmxSequenceNumber = lighting->sequence_number();
			Bus::LightingFrame frame;
			frame.watermark = lighting->audio_frame_position();
			std::memcpy(frame.channels.data(), lighting->channels().data(), DmxFrameSize);
			if (!mDmxQueue.Push(frame))
				++mDmxDrops;
			mDmxAt = now;
		}
	});
	now = SteadyMs();
	if (mLink.Expired(now))
		throw std::runtime_error("transmitter heartbeat timed out after 30 seconds");
	if (!mLogin)
		return;
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
	if (now - mHealthAt >= Bus::HealthReportIntervalMilliseconds)
	{
		const auto& audio = mAudio->playout;
		Bus::Metrics health;
		health.set_run_id(mConfig.mRunId);
		health.set_last_processed_command_id(mLastProcessedCommandId);
		health.set_up(true);
		health.set_link_up(mLink.Up(now));
		health.set_heartbeat_age_seconds(mLink.Age(now));
		health.set_heartbeats_sent(mHeartbeatsSent);
		health.set_heartbeats_received(mHeartbeatsReceived);
		health.set_audio_buffer_seconds(audio.bufferedFrames.load() / double(mConfig.mAudioSampleRate));
		health.set_buffer_target_seconds(mConfig.mJitterBufferMs / double(MillisecondsPerSecond));
		health.set_underruns_total(audio.underruns.load());
		health.set_device_underruns_total(mAudio->deviceUnderruns.load());
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
		mBus.Send(health);
		WriteHealth({{"run_id", mConfig.mRunId}, {"rx", Bus::MetricsToJson(health)}});
		mHealthAt = now;
		mAudioMaxGap = 0;
		mAudioPeak = 0;
	}
}

} // namespace Radius
