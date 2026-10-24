#include "clock.h"

#ifdef WIN32
#include <windows.h>
#endif

namespace DmxEnttecNode::tsc_impl {


uint64_t rdtscp()
{
#if defined(WIN32) // WINDOWS
    return GetTickCount64();
#elif defined(__arm64__) // ARM64
    uint64_t val;
    __asm__ __volatile__("mrs %0, cntvct_el0" : "=r" (val));
    return val;
#else // x86-64
    uint64_t rax, rcx, rdx;
	__asm__ __volatile__("rdtscp" : "=a"(rax), "=d"(rdx), "=c"(rcx));
	return (rdx << 32) + rax;
#endif
}

void cpuid()
{
#if defined(WIN32) // WINDOWS
    // TODO: impl for windows
#elif defined(__arm64__) // ARM64
    // TODO: impl for mac
#else // x86-64
    uint64_t rax, rbx, rcx, rdx;
    __asm__ __volatile__("cpuid" : "=a"(rax), "=b"(rbx), "=d"(rdx), "=c"(rcx));
#endif
}

uint64_t rdtscp(int& chip, int& core)
{
#if defined(WIN32) // WINDOWS
    // TODO: revise, this probably does not work
    return GetTickCount64();
#elif defined(__arm64__) // ARM64
    // TODO: revise, this probably does not work
    uint64_t val;
    __asm__ __volatile__("mrs %0, cntvct_el0" : "=r" (val));
    return val;
#else // x86-64
    uint64_t rax, rcx, rdx;
	__asm__ __volatile__("rdtscp" : "=a"(rax), "=d"(rdx), "=c"(rcx));
	chip = static_cast<int>((rcx & 0xFFF000) >> 12);
	core = static_cast<int>(rcx & 0xFFF);
	return (rdx << 32) + rax;
#endif
}

}