#pragma once
#include <config/config.h>
#include <soundio/soundio.h>
#include "playout.h"
namespace Radius {
class AudioPlayback {
    const Config& mConfig;
    SoundIo* mIo=nullptr;
    SoundIoDevice* mDevice=nullptr;
    SoundIoOutStream* mStream=nullptr;
    bool mThreadConfigured=false;
    static void Write(SoundIoOutStream*,int,int) noexcept;
public:
    Playout playout;
    std::atomic<int> error {0};
    std::atomic<uint64_t> deviceUnderruns {0}, audibleWatermark {0};
    explicit AudioPlayback(const Config& c):mConfig(c),playout(c.mAudioSampleRate,c.mJitterBufferMs) {}
    ~AudioPlayback();
    void Start();
};
}
