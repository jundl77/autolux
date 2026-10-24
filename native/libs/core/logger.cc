#include "logger.h"

namespace DmxEnttecNode {

static uint8_t GLOBAL_LOG_LEVEL = LL_INFO;

void SetGlobalLogLevel(int logLevel)
{
	GLOBAL_LOG_LEVEL = logLevel;
}

uint8_t GetGlobalLogLevel()
{
	return GLOBAL_LOG_LEVEL;
}

std::string LogLevelToString(int logLevel)
{
	switch (logLevel)
	{
		case LL_DEBUG: return "Debug";
		case LL_INFO: return "Info";
		case LL_WARN: return "Warn";
		case LL_ERROR: return "Error";
	}
	throw std::runtime_error("unknown log-level: " + std::to_string(logLevel));
}

}