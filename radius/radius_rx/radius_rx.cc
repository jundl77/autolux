#include "radius_rx.h"
#include <core/thread_config.h>
#include <enttec/usb_client.h>
#include <future>
namespace Radius {
RadiusRx::~RadiusRx() { mDmxLoop.Stop(); if(mDmxThread.joinable()) mDmxThread.join(); }
void RadiusRx::Start() {
    ConfigureThread(mConfig.mCoreAffinity);
    mBus.Open(mConfig.mBindAddress,mConfig.mPeerAddress,mConfig.mBusPort,true);
    if(mConfig.mDmxEnabled) StartDmx();
    if(mConfig.mAudioEnabled) mAudio.Start();
    mPoll=mLoop.AddPoller([this]{ Tick(); });
}
void RadiusRx::StartDmx() {
    std::promise<void> ready; auto started=ready.get_future();
    mDmxThread=std::thread([this,ready=std::move(ready)]() mutable {
        bool signalled=false;
        try {
            ConfigureThread(mConfig.mDmxCore);
            EnttecUSBClient usb(mDmxLoop); usb.Start(); mDmxConnected=true;
            bool dark=true;
            auto timer=mDmxLoop.AddTimer(5ms,[&] {
                const auto now=SteadyMs();
                const bool stale=!mDmxAt || now-mDmxAt.load()>1000 ||
                    (mConfig.mAudioEnabled && (!mAudioAt || now-mAudioAt.load()>1000));
                if(stale) {
                    if(!dark) { std::array<uint8_t,512> zero{}; usb.UpdateFrame(DmxFrame(zero)); dark=true; }
                    while(mDmxQueue.Front()) mDmxQueue.Pop();
                    return;
                }
                const auto watermark=mAudio.audibleWatermark.load();
                std::array<uint8_t,512> frame{}; bool changed=false;
                while(const auto* p=mDmxQueue.Front()) {
                    if(mConfig.mAudioEnabled && p->watermark>watermark) break;
                    std::memcpy(frame.data(),p->payload.data(),512);
                    mDmxLateness=watermark>p->watermark ? watermark-p->watermark : 0;
                    mDmxQueue.Pop(); changed=true;
                }
                if(changed) { usb.UpdateFrame(DmxFrame(frame)); ++mDmxFrames; dark=false; }
            });
            ready.set_value(); signalled=true; mDmxLoop.Run(RunHot::No);
        } catch(...) {
            mDmxFailed=true;
            if(!signalled) ready.set_exception(std::current_exception());
        }
        mDmxConnected=false;
    });
    started.get();
}
void RadiusRx::Tick() {
    const auto now=SteadyMs();
    if(mAudio.error) throw std::runtime_error("audio playback failed: "+std::to_string(mAudio.error.load()));
    if(mDmxFailed) throw std::runtime_error("DMX output worker failed");
    mBus.Poll(mConfig.mSession,[&](const Bus::Packet& p) {
        if(p.kind==Bus::Kind::Heartbeat) { mLink.Received(now); ++mHeartbeatsReceived; }
        else if(p.kind==Bus::Kind::Audio && mConfig.mAudioEnabled) {
            Bus::AudioBlock block; block.watermark=p.watermark; block.frames=p.size/8;
            std::memcpy(block.samples.data(),p.payload.data(),p.size);
            for(size_t i=0;i<block.frames*2;++i) mAudioPeak=(std::max)(mAudioPeak,std::abs(block.samples[i]));
            if(mAudio.playout.Push(block)) {
                const auto previous=mAudioAt.load();
                if(previous) mAudioMaxGap=(std::max)(mAudioMaxGap,now-previous);
                mAudioAt=now;
            }
        } else if(p.kind==Bus::Kind::Dmx && mConfig.mDmxEnabled) {
            if(mLastDmxSequence && static_cast<int32_t>(p.sequence-mLastDmxSequence)<=0) { ++mDmxLate; return; }
            mLastDmxSequence=p.sequence;
            if(!mDmxQueue.Push(p)) ++mDmxDrops;
            mDmxAt=now;
        }
    });
    if(mLink.Expired(now)) throw std::runtime_error("transmitter heartbeat timed out after 30 seconds");
    if(mLink.Due(now)) {
        Bus::Packet p; p.session=mConfig.mSession;
        if(mBus.Send(p)) { mLink.Sent(now); ++mHeartbeatsSent; }
    }
    if(now-mHealthAt>=1000) {
        const auto& audio=mAudio.playout;
        njson health={{"up",true},{"link_up",mLink.Up(now)},
            {"heartbeat_age_seconds",mLink.Age(now)},{"heartbeats_sent",mHeartbeatsSent},{"heartbeats_received",mHeartbeatsReceived},
            {"audio_buffer_seconds",audio.bufferedFrames.load()/double(mConfig.mAudioSampleRate)},
            {"buffer_target_seconds",mConfig.mJitterBufferMs/1000.0},
            {"underruns_total",audio.underruns.load()},{"device_underruns_total",mAudio.deviceUnderruns.load()},
            {"concealed_frames_total",audio.concealedFrames.load()},{"lost_frames_total",audio.lostFrames.load()},
            {"overflow_frames_total",audio.overflowFrames.load()},{"late_packets_total",audio.latePackets.load()},
            {"latency_trims_total",audio.latencyTrims.load()},{"clock_correction_ppm",audio.driftPpm.load()},
            {"dmx_connected",mDmxConnected.load()},{"dmx_frames_total",mDmxFrames.load()},
            {"dmx_dropped_total",mDmxDrops+mDmxLate},{"dmx_lateness_seconds",mDmxLateness.load()/double(mConfig.mAudioSampleRate)},
            {"audio_input_age_seconds",mAudioAt ? njson((now-mAudioAt.load())/1000.0) : njson(nullptr)},
            {"audio_max_gap_seconds",mAudioAt ? njson((std::max)(mAudioMaxGap,now-mAudioAt.load())/1000.0) : njson(nullptr)},
            {"audio_signal_peak",mAudioPeak},
            {"invalid_packets_total",mBus.invalidPackets},{"send_failures_total",mBus.sendFailures}};
        Bus::Packet p; p.kind=Bus::Kind::Health; p.session=mConfig.mSession;
        const auto serialized=health.dump();
        if(serialized.size()>Bus::MaxPayload) throw std::runtime_error("health payload exceeds MTU budget");
        p.size=static_cast<uint16_t>(serialized.size()); std::memcpy(p.payload.data(),serialized.data(),p.size);
        mBus.Send(p); WriteHealth({{"session",mConfig.mSession},{"rx",health}}); mHealthAt=now; mAudioMaxGap=0; mAudioPeak=0;
    }
}
}
