#pragma once
#include <network/bus_socket.h>
#include <network/link_state.h>
#include <core/app.h>
#include <core/health_file.h>
#include <core/spsc_queue.h>
#include <soundio/audio_playback.h>
#include <thread>
namespace Radius {
class RadiusRx : public App {
    const Config& mConfig;
    EventLoop& mLoop;
    BusSocket mBus;
    LinkState mLink{SteadyMs()};
    AudioPlayback mAudio;
    EventLoop mDmxLoop;
    std::thread mDmxThread;
    SpscQueue<Bus::Packet,256> mDmxQueue;
    std::atomic<bool> mDmxConnected{false},mDmxFailed{false};
    std::atomic<uint64_t> mDmxFrames{0},mDmxAt{0},mAudioAt{0};
    std::atomic<int64_t> mDmxLateness{0};
    uint64_t mHealthAt=0,mDmxDrops=0,mDmxLate=0,mHeartbeatsSent=0,mHeartbeatsReceived=0;
    uint64_t mAudioMaxGap=0;
    float mAudioPeak=0;
    uint32_t mLastDmxSequence=0;
    ScopedHandler mPoll;
    void Tick();
    void StartDmx();
public:
    RadiusRx(const Config& c,EventLoop& loop):mConfig(c),mLoop(loop),mAudio(c) {}
    ~RadiusRx() override;
    void Start() override;
    const Config& GetConfig() const override { return mConfig; }
};
}
