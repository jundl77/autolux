#pragma once

#include "clock.h"

#include <functional>
#include <chrono>

using TimerCallback = std::function<void()>;

namespace Radius {

class Timer
{
public:
	explicit Timer(std::chrono::nanoseconds interval)
	    : mInterval(interval)
	{
	}

	void Start(TimerCallback callback)
	{
		mCallback = callback;
		mLastCall = Clock::Now();
	}

	void Stop()
	{
		mCallback = {};
	}

	bool Active() const
	{
		return bool(mCallback);
	}

	void Poll(MonotonicTime now)
	{
		if (ShouldRun(now))
		{
			Run(now);
		}
	}

private:
	bool ShouldRun(MonotonicTime now) const
	{
		return mCallback && now - mLastCall > mInterval;
	}

	void Run(MonotonicTime now)
	{
		mLastCall = now;
		auto callback = mCallback;
		callback(); // The callback may cancel its own timer.
	}

	std::chrono::nanoseconds mInterval;
	TimerCallback mCallback;

	MonotonicTime mLastCall{};
};

} // namespace Radius
