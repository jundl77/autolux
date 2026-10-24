#pragma once

#include "clock.h"

#include <functional>
#include <chrono>

using TimerCallback = std::function<void()>;

namespace DmxEnttecNode {

class Timer
{
public:
	explicit Timer(std::chrono::nanoseconds interval)
		: mInterval(interval)
	{}

	void Start(TimerCallback callback)
	{
		mCallback = callback;
		mLastCall = Clock::Now();
	}

	void Stop()
	{
		mCallback = {};
	}

	void Poll(NanoPosixTime now)
	{
		if (ShouldRun(now))
		{
			Run(now);
		}
	}

private:
	bool ShouldRun(NanoPosixTime now) const
	{
		return mCallback && now - mLastCall > mInterval;
	}

	void Run(NanoPosixTime now)
	{
		mCallback();
		mLastCall = now;
	}

	std::chrono::nanoseconds mInterval;
	TimerCallback mCallback;

	NanoPosixTime mLastCall {};
};

}