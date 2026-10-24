#pragma once
#include <chrono>
#include <thread>

namespace Radius {

using namespace std::chrono_literals;
using MonotonicTime = std::chrono::nanoseconds;

class Clock
{
public:
	static void Initialise()
	{
	}

	static MonotonicTime Now()
	{
		return std::chrono::duration_cast<MonotonicTime>(std::chrono::steady_clock::now().time_since_epoch());
	}
};

} // namespace Radius
