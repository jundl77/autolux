#pragma once
#include <windows.h>
#include <stdexcept>

namespace Radius {

inline void ConfigureThread(int core)
{
	if (core < 0)
		return;
	DWORD_PTR process = 0, system = 0;
	if (core >= sizeof(DWORD_PTR) * 8 || !GetProcessAffinityMask(GetCurrentProcess(), &process, &system) ||
	    !(process & (DWORD_PTR{1} << core)) || !SetThreadAffinityMask(GetCurrentThread(), DWORD_PTR{1} << core))
		throw std::runtime_error("requested CPU core is unavailable");
}

} // namespace Radius
