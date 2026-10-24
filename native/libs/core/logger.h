#pragma once

#include "clock.h"
#include "strong_typedef.h"
#include <stdexcept>
#include <iostream>
#include <cstdio>
#include <format>
#include <chrono>

namespace DmxEnttecNode {

#define LL_DEBUG 0
#define LL_INFO  1
#define LL_WARN  2
#define LL_ERROR 3

using LogModule = StrongTypedef<std::string, struct LogModuleTag>;

void SetGlobalLogLevel(int logLevel);
uint8_t GetGlobalLogLevel();

std::string LogLevelToString(int logLevel);

inline decltype(auto) GetLocalTime()
{
	std::chrono::zoned_time time {std::chrono::current_zone(), std::chrono::system_clock::now()};
	return std::format("{:%Y-%m-%d %T}", time);
}

#define LOG(level, module, ...) \
	do \
	{ \
		if (level == LL_DEBUG && GetGlobalLogLevel() == LL_DEBUG) \
		{ \
			std::cout << GetLocalTime() << " [Debug ] [" << module << "] "; \
			std::printf(__VA_ARGS__); \
			std::cout << std::endl; \
		} \
		else if (level == LL_INFO && GetGlobalLogLevel() <= LL_INFO) \
		{ \
			std::cout << GetLocalTime() << " [Info ] [" << module << "] "; \
			std::printf(__VA_ARGS__); \
			std::cout << std::endl; \
		} \
		else if (level == LL_WARN && GetGlobalLogLevel() <= LL_WARN) \
		{ \
			std::cout << GetLocalTime() << " [Warn ] [" << module << "] "; \
			std::printf(__VA_ARGS__); \
			std::cout << std::endl; \
		} \
		else if (level == LL_ERROR && GetGlobalLogLevel() <= LL_ERROR) \
		{ \
			std::cout << GetLocalTime() << " [Error ] [" << module << "] "; \
			std::printf(__VA_ARGS__); \
			std::cout << std::endl; \
		} \
	} while(false)

#ifdef NDEBUG
#define DEBUG_LOG(level, module, ...) {}
#else
#define DEBUG_LOG(level, module, ...) LOG(level, module, __VA_ARGS__)
#endif

}