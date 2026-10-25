#pragma once
#include <core/spsc_queue.h>
#include <core/clock.h>
#include <bus/bus_protocol.h>
#include <algorithm>
#include <atomic>
#include <cmath>

namespace Radius {

// Network producer; audio callback consumer. Fixed storage, no locks or allocation.
class Playout
{
	static constexpr size_t QueueCapacityBlocks = 256;
	static constexpr int LatencyRecoveryTargetMultiple = 4;
	static constexpr double ClockCorrectionGain = 0.01;
	static constexpr double MaxClockCorrectionRatio = 0.001;
	static constexpr double ClockCorrectionSmoothing = 0.02;
	static constexpr int PartsPerMillion = 1000000;
	static constexpr int StarvationTimeoutMilliseconds = 250;
	static constexpr uint64_t InterpolationLookaheadFrames = 2;
	SpscQueue<Bus::AudioBlock, QueueCapacityBlocks> mQueue;
	uint64_t mNextExpectedAudioFrameIndex = 0, mCursor = 0;
	size_t mOffset = 0;
	int mRate, mTarget;
	bool mPrimed = false, mStarved = false;
	float mCurrent[Bus::Channels]{}, mNext[Bus::Channels]{};
	double mPhase = 0, mRatio = 1;
	uint64_t mMissingRun = 0;
	std::atomic<uint64_t> mLatest{0};

	bool Take(float* frame) noexcept
	{
		for (;;)
		{
			const auto* block = mQueue.Front();
			if (!block)
			{
				frame[0] = frame[1] = 0;
				return false;
			}
			if (block->watermark + block->frames <= mCursor)
			{
				mQueue.Pop();
				mOffset = 0;
				continue;
			}
			if (block->watermark > mCursor)
			{
				frame[0] = frame[1] = 0;
				++mCursor;
				++concealedFrames;
				return true;
			}
			mOffset = static_cast<size_t>(mCursor - block->watermark);
			frame[0] = block->samples[Bus::Channels * mOffset];
			frame[1] = block->samples[Bus::Channels * mOffset + 1];
			++mCursor;
			++mOffset;
			if (mOffset == block->frames)
			{
				mQueue.Pop();
				mOffset = 0;
			}
			return true;
		}
	}

public:
	std::atomic<uint64_t> overflowFrames{0}, latePackets{0}, lostFrames{0};
	std::atomic<uint64_t> underruns{0}, concealedFrames{0}, latencyTrims{0};
	std::atomic<uint64_t> played{0};
	std::atomic<int> bufferedFrames{0}, driftPpm{0};

	explicit Playout(int rate, int targetMs)
	    : mRate(rate)
	    , mTarget(rate * targetMs / MillisecondsPerSecond)
	{
	}

	bool Push(const Bus::AudioBlock& block) noexcept
	{
		if (!block.frames || block.frames > Bus::AudioFrames || block.watermark > UINT64_MAX - block.frames)
			return false;
		if (block.watermark < mNextExpectedAudioFrameIndex)
		{
			++latePackets;
			return false;
		}
		if (mNextExpectedAudioFrameIndex && block.watermark > mNextExpectedAudioFrameIndex)
			lostFrames += block.watermark - mNextExpectedAudioFrameIndex;
		mNextExpectedAudioFrameIndex = block.watermark + block.frames;
		if (!mQueue.Push(block))
		{
			overflowFrames += block.frames;
			return false;
		}
		mLatest.store(mNextExpectedAudioFrameIndex, std::memory_order_release);
		return true;
	}

	void BeginCallback() noexcept
	{
		// Correct slow device clock differences continuously. Large backlog is recovered
		// explicitly, so a transient stall cannot turn into seconds of permanent latency.
		const auto latest = mLatest.load();
		const auto* first = mQueue.Front();
		const auto bufferedStartFrame = mPrimed ? mCursor : (first ? first->watermark : latest);
		if (latest > bufferedStartFrame &&
		    latest - bufferedStartFrame > uint64_t(mTarget) * LatencyRecoveryTargetMultiple)
		{
			const auto keepFrom = latest - mTarget;
			while (auto* block = mQueue.Front())
			{
				if (block->watermark + block->frames >= keepFrom)
					break;
				mQueue.Pop();
				++latencyTrims;
			}
			mPrimed = false;
			mOffset = 0;
		}
		if (!mPrimed)
		{
			first = mQueue.Front();
			bufferedFrames = first && latest >= first->watermark ? static_cast<int>(latest - first->watermark) : 0;
			if (bufferedFrames < mTarget)
				return;
			mCursor = first->watermark;
			mPhase = 0;
			mMissingRun = 0;
			Take(mCurrent);
			Take(mNext);
			mPrimed = true;
		}
		const int fill = latest > mCursor ? static_cast<int>(latest - mCursor) : 0;
		bufferedFrames = fill;
		const double desired = 1.0 + std::clamp((fill - mTarget) / double(mRate) * ClockCorrectionGain,
		                                        -MaxClockCorrectionRatio, MaxClockCorrectionRatio);
		mRatio += ClockCorrectionSmoothing * (desired - mRatio);
		driftPpm = static_cast<int>((mRatio - 1) * PartsPerMillion);
	}

	void Frame(float* output) noexcept
	{
		if (!mPrimed)
		{
			output[0] = output[1] = 0;
			return;
		}
		for (size_t ch = 0; ch < Bus::Channels; ++ch)
			output[ch] = static_cast<float>((1 - mPhase) * mCurrent[ch] + mPhase * mNext[ch]);
		mPhase += mRatio;
		while (mPhase >= 1)
		{
			mPhase -= 1;
			mCurrent[0] = mNext[0];
			mCurrent[1] = mNext[1];
			if (!Take(mNext))
			{
				if (!mStarved)
					++underruns;
				mStarved = true;
				++concealedFrames;
				++mMissingRun;
				if (mMissingRun > uint64_t(mRate * StarvationTimeoutMilliseconds / MillisecondsPerSecond))
					mPrimed = false;
			}
			else
			{
				mStarved = false;
				mMissingRun = 0;
			}
		}
		played.store(mCursor > InterpolationLookaheadFrames ? mCursor - InterpolationLookaheadFrames : 0,
		             std::memory_order_release);
	}
};

} // namespace Radius
