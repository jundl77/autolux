#pragma once

#include "timer.h"
#include "scoped_handler.h"
#include "clock.h"

#include <functional>
#include <vector>
#include <utility>
#include <memory>

namespace DmxEnttecNode {

enum class RunHot { Yes, No };

class EventLoop
{
public:
	int Run(RunHot);

	using Callback = std::function<void()>;
	void Post(std::chrono::nanoseconds, Callback);

	ScopedHandler AddTimer(std::chrono::nanoseconds, Callback);
	ScopedHandler AddPoller(Callback);

private:
	std::vector<std::pair<NanoPosixTime, Callback>> mCallbacks;
	std::vector<std::unique_ptr<Timer>> mTimers;
};

}