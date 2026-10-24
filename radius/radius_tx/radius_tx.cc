#include "radius_tx.h"
#include <core/thread_config.h>
namespace Radius {
void RadiusTx::Start() {
    ConfigureThread(mConfig.mCoreAffinity);
    mBus.Open(mConfig.mBindAddress,mConfig.mPeerAddress,mConfig.mBusPort,false);
    if(mConfig.mArtnetEnabled) { mArtnet.StartListening(); mOverlay.Start(); }
    if(mConfig.mAudioEnabled) mAudio.Start();
    mPoll=mLoop.AddPoller([this]{ Tick(); });
}
void RadiusTx::Tick() {
    const auto now=SteadyMs();
    if(mAudio.error) throw std::runtime_error("audio capture failed: "+std::to_string(mAudio.error.load()));
    mBus.Poll(mConfig.mSession,[&](const Bus::Packet& p) {
        if(p.kind==Bus::Kind::Heartbeat) { mLink.Received(now); ++mHeartbeatsReceived; }
        if(p.kind==Bus::Kind::Health) {
            auto health=njson::parse(p.payload.begin(),p.payload.begin()+p.size,nullptr,false);
            if(health.is_object()) { mReceiver=std::move(health); mReceiverAt=now; }
        }
    });
    if(mLink.Expired(now)) throw std::runtime_error("receiver heartbeat timed out after 30 seconds");
    if(mLink.Due(now)) {
        Bus::Packet p; p.session=mConfig.mSession;
        if(mBus.Send(p)) { mLink.Sent(now); ++mHeartbeatsSent; }
    }
    for(int i=0;i<128;++i) {
        const auto* block=mAudio.Front(); if(!block) break;
        Bus::Packet p; p.kind=Bus::Kind::Audio; p.session=mConfig.mSession;
        p.watermark=block->watermark; p.sequence=++mAudioSequence; p.size=block->frames*8;
        std::memcpy(p.payload.data(),block->samples.data(),p.size);
        mBus.Send(p); mAudio.Pop();
    }
    if(mConfig.mArtnetEnabled && now-mLastDmxSend>=25) {
        Bus::Packet p; p.kind=Bus::Kind::Dmx; p.session=mConfig.mSession;
        p.watermark=mAudio.watermark.load(); p.sequence=++mDmxSequence; p.size=512;
        if(mDmxAt && now-mDmxAt<3000) { auto frame=mDmx.Compose(); std::memcpy(p.payload.data(),frame.data(),512); }
        mBus.Send(p); mLastDmxSend=now;
    }
    if(now-mHealthAt>=1000) {
        njson tx={{"up",true},{"link_up",mLink.Up(now)},{"heartbeat_age_seconds",mLink.Age(now)},
            {"heartbeats_sent",mHeartbeatsSent},{"heartbeats_received",mHeartbeatsReceived},
            {"audio_buffer_seconds",mAudio.QueuedFrames()/double(mConfig.mAudioSampleRate)},
            {"capture_overruns_total",mAudio.captureOverruns.load()},{"queue_dropped_frames_total",mAudio.droppedFrames.load()},
            {"send_failures_total",mBus.sendFailures},{"invalid_packets_total",mBus.invalidPackets},
            {"playback_delay_seconds",mConfig.mPlaybackDelaySeconds},
            {"dmx_input_age_seconds",mDmxAt ? njson((now-mDmxAt)/1000.0) : njson(nullptr)}};
        WriteHealth({{"session",mConfig.mSession},{"tx",tx},{"rx",mReceiver},
            {"rx_age_seconds",mReceiverAt ? njson((now-mReceiverAt)/1000.0) : njson(nullptr)}});
        mHealthAt=now;
    }
}
void RadiusTx::OnDmxMessage(const DmxFrame& frame,int universe) {
    if(universe!=mConfig.mDmxUniverse) return;
    mDmx.Base(frame); mDmxAt=SteadyMs();
}
void RadiusTx::OnOverlayMessage(const OverlayIdl::OverlayMessage& overlay) {
    if(overlay.mUniverse==mConfig.mDmxUniverse) mDmx.Overlay(overlay);
}
}
