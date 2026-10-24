#include "event_loop.h"
#include "clock.h"

#include <algorithm>

namespace Radius {

using namespace std::chrono_literals;

EventLoop::EventLoop() = default;

int EventLoop::Run(RunHot hot)
{
	while (!mPosted->stop)
	{
		const MonotonicTime now = Clock::Now();
		{
			std::lock_guard<std::mutex> lock(mPosted->mutex);
			for (auto& callback : mPosted->callbacks)
				mCallbacks.push_back(std::move(callback));
			mPosted->callbacks.clear();
		}
		std::vector<Callback> ready;

		mCallbacks.erase(std::remove_if(mCallbacks.begin(), mCallbacks.end(),
			[&](const std::pair<MonotonicTime, Callback>& p)
			{
				const bool expired = p.first < now;
				if (expired)
				{
					ready.push_back(p.second);
				}
				return expired;
			}), mCallbacks.end());
		// Callbacks may post more work; never call them while mutating the queue.
		for (auto& callback : ready)
			callback();

		const auto timerCount = mTimers.size();
		for (size_t i = 0; i < timerCount; ++i)
		{
			mTimers[i]->Poll(now);
		}
		mTimers.erase(std::remove_if(mTimers.begin(), mTimers.end(),
			[](const auto& timer) { return !timer->Active(); }), mTimers.end());

		if (hot == RunHot::No)
		{
			std::this_thread::sleep_for(std::chrono::milliseconds(1));
		}
	}

	return 0;
}

void EventLoop::Post(std::chrono::nanoseconds duration, Callback callback)
{
	const MonotonicTime ts = Clock::Now() + duration;
	std::lock_guard<std::mutex> lock(mPosted->mutex);
	mPosted->callbacks.emplace_back(ts, std::move(callback));
}

ScopedHandler EventLoop::AddTimer(std::chrono::nanoseconds interval, Callback callback)
{
	mTimers.push_back(std::make_unique<Timer>(interval));

	Timer& newTimer = *mTimers.back();
	newTimer.Start(std::move(callback));

	Timer* ptrTimer = mTimers.back().get();
	return ScopedHandler([this, ptrTimer]()
	{
		ptrTimer->Stop(); // Reclaim after polling, outside any active callback.
	});
}

ScopedHandler EventLoop::AddPoller(Callback callback)
{
	return AddTimer(0ns, callback);
}

}
