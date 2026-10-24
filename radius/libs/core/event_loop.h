#pragma once

#include "timer.h"
#include "scoped_handler.h"
#include "clock.h"

#include <functional>
#include <vector>
#include <utility>
#include <memory>
#include <mutex>
#include <atomic>

namespace Radius {

enum class RunHot
{
	Yes,
	No
};

class EventLoop
{
public:
	EventLoop();
	EventLoop(EventLoop&&) = default;
	int Run(RunHot);

	void Stop()
	{
		mPosted->stop = true;
	}

	using Callback = std::function<void()>;
	void Post(std::chrono::nanoseconds, Callback);

	ScopedHandler AddTimer(std::chrono::nanoseconds, Callback);
	ScopedHandler AddPoller(Callback);

private:
	struct Posted
	{
		std::mutex mutex;
		std::vector<std::pair<MonotonicTime, Callback>> callbacks;
		std::atomic<bool> stop{false};
	};

	std::unique_ptr<Posted> mPosted = std::make_unique<Posted>();
	std::vector<std::pair<MonotonicTime, Callback>> mCallbacks;
	std::vector<std::unique_ptr<Timer>> mTimers;
};

} // namespace Radius
