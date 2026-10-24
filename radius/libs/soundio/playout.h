#pragma once
#include <core/spsc_queue.h>
#include <proto/bus.h>
#include <algorithm>
#include <atomic>
#include <cmath>

namespace Radius {
// Network producer; audio callback consumer. Fixed storage, no locks or allocation.
class Playout {
    SpscQueue<Bus::AudioBlock, 256> mQueue;
    uint64_t mAcceptedEnd=0, mCursor=0;
    size_t mOffset=0;
    int mRate, mTarget;
    bool mPrimed=false, mStarved=false;
    float mCurrent[2]{}, mNext[2]{};
    double mPhase=0, mRatio=1;
    uint64_t mMissingRun=0;
    std::atomic<uint64_t> mLatest {0};
    bool Take(float* frame) noexcept {
        for (;;) {
            const auto* block=mQueue.Front();
            if (!block) { frame[0]=frame[1]=0; return false; }
            if (block->watermark+block->frames<=mCursor) { mQueue.Pop(); mOffset=0; continue; }
            if (block->watermark>mCursor) {
                frame[0]=frame[1]=0; ++mCursor; ++concealedFrames; return true;
            }
            mOffset=static_cast<size_t>(mCursor-block->watermark);
            frame[0]=block->samples[2*mOffset]; frame[1]=block->samples[2*mOffset+1];
            ++mCursor; ++mOffset;
            if (mOffset==block->frames) { mQueue.Pop(); mOffset=0; }
            return true;
        }
    }
public:
    std::atomic<uint64_t> overflowFrames {0}, latePackets {0}, lostFrames {0};
    std::atomic<uint64_t> underruns {0}, concealedFrames {0}, latencyTrims {0};
    std::atomic<uint64_t> played {0};
    std::atomic<int> bufferedFrames {0}, driftPpm {0};
    explicit Playout(int rate, int targetMs) : mRate(rate), mTarget(rate*targetMs/1000) {}
    bool Push(const Bus::AudioBlock& block) noexcept {
        if (!block.frames || block.frames>Bus::AudioFrames || block.watermark>UINT64_MAX-block.frames) return false;
        if (block.watermark < mAcceptedEnd) { ++latePackets; return false; }
        if (!mQueue.Push(block)) { overflowFrames+=block.frames; return false; }
        if (mAcceptedEnd && block.watermark>mAcceptedEnd) lostFrames+=block.watermark-mAcceptedEnd;
        mAcceptedEnd=block.watermark+block.frames;
        mLatest.store(mAcceptedEnd, std::memory_order_release);
        return true;
    }
    void BeginCallback() noexcept {
        // Correct slow device clock differences continuously. Large backlog is recovered
        // explicitly, so a transient stall cannot turn into seconds of permanent latency.
        if (mPrimed && mLatest.load()>mCursor+uint64_t(mTarget*4)) {
            const auto keepFrom=mLatest.load()-mTarget;
            while (auto* block=mQueue.Front()) {
                if (block->watermark+block->frames>=keepFrom) break;
                mQueue.Pop(); ++latencyTrims;
            }
            mPrimed=false; mOffset=0;
        }
        if (!mPrimed) {
            const auto* first=mQueue.Front();
            const auto latest=mLatest.load();
            if (!first || latest<first->watermark || latest-first->watermark<uint64_t(mTarget)) return;
            mCursor=first->watermark; mPhase=0; mMissingRun=0;
            Take(mCurrent); Take(mNext); mPrimed=true;
        }
        const auto latest=mLatest.load();
        const int fill=latest>mCursor ? static_cast<int>(latest-mCursor) : 0;
        bufferedFrames=fill;
        const double desired=1.0+std::clamp((fill-mTarget)/double(mRate)*0.01, -0.001, 0.001);
        mRatio+=0.02*(desired-mRatio);
        driftPpm=static_cast<int>((mRatio-1)*1000000);
    }
    void Frame(float* output) noexcept {
        if (!mPrimed) { output[0]=output[1]=0; return; }
        for (int ch=0;ch<2;++ch) output[ch]=mCurrent[ch]+float(mPhase)*(mNext[ch]-mCurrent[ch]);
        mPhase+=mRatio;
        while (mPhase>=1) {
            mPhase-=1; mCurrent[0]=mNext[0]; mCurrent[1]=mNext[1];
            if (!Take(mNext)) {
                if (!mStarved) ++underruns;
                mStarved=true; ++concealedFrames; ++mMissingRun;
                if (mMissingRun>uint64_t(mRate/4)) mPrimed=false;
            } else { mStarved=false; mMissingRun=0; }
        }
        played.store(mCursor>2 ? mCursor-2 : 0, std::memory_order_release);
    }
};
}
