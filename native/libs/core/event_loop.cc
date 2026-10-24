#include "event_loop.h"
#include "clock.h"

#include <algorithm>

namespace DmxEnttecNode {

using namespace std::chrono_literals;

int EventLoop::Run(RunHot hot)
{
	while (true)
	{
		const NanoPosixTime now = Clock::Now();

		mCallbacks.erase(std::remove_if(mCallbacks.begin(), mCallbacks.end(),
			[=](const std::pair<NanoPosixTime, Callback>& p)
			{
				const bool expired = p.first < now;
				if (expired)
				{
					p.second();
				}
				return expired;
			}), mCallbacks.end());

		for (auto& timer : mTimers)
		{
			timer->Poll(now);
		}

		if (hot == RunHot::No)
		{
			std::this_thread::sleep_for(std::chrono::milliseconds(1));
		}
	}

	return 0;
}

void EventLoop::Post(std::chrono::nanoseconds duration, Callback callback)
{
	const NanoPosixTime ts = Clock::Now() + duration;
	mCallbacks.emplace_back(ts, std::move(callback));
}

ScopedHandler EventLoop::AddTimer(std::chrono::nanoseconds interval, Callback callback)
{
	mTimers.push_back(std::make_unique<Timer>(interval));

	Timer& newTimer = *mTimers.back();
	newTimer.Start(std::move(callback));

	const Timer* ptrTimer = mTimers.back().get();
	return ScopedHandler([this, ptrTimer]()
	{
		mTimers.erase(std::find_if(mTimers.begin(), mTimers.end(), [&](auto&& uptr) { return uptr.get() == ptrTimer; }));
	});
}

ScopedHandler EventLoop::AddPoller(Callback callback)
{
	return AddTimer(0ns, callback);
}

}
