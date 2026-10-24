#include "app.h"
#include <core/logger.h>
#include <core/clock.h>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>
#include <optional>

#ifdef WIN32
#include <Windows.h>
#endif

namespace DmxEnttecNode {

namespace {

const LogModule LM_MAIN {"MAIN"};

std::optional<Config> LoadConfig(int argc, char *argv[], AppType appType)
{
	std::string filePath = "dmx_usb_node.json";
	if (argc == 2)
	{
		filePath = argv[1];
	}
	else if (argc > 2)
	{
		LOG(LL_ERROR, LM_MAIN, "unable to parse arguments, usage: %s [config_file]",  argv[0]);
		return std::nullopt;
	}

	return Config::FromFile(filePath, appType);
}

void SetAffinity(int core)
{
	bool success = false;
#ifdef WIN32
	HANDLE process = GetCurrentProcess();
	DWORD_PTR processAffinityMask = 1 << core;

	BOOL affinitySuccess = SetProcessAffinityMask(process, processAffinityMask);
	BOOL prioSuccess = SetPriorityClass(process, REALTIME_PRIORITY_CLASS);
	success = affinitySuccess && prioSuccess;
#else
	return false;
#endif

	LOG(LL_INFO, LM_MAIN, "set core affinity to core: %d (success=%d)", core, success);
}

}

std::optional<AppContext> SetupApp(int argc, char *argv[], const std::string& appName, AppType appType)
{
	LOG(LL_INFO, LM_MAIN, "==================================================");
	LOG(LL_INFO, LM_MAIN, "Starting %s engine", appName.c_str());
	LOG(LL_INFO, LM_MAIN, "==================================================");
	LOG(LL_INFO, LM_MAIN, "");
	LOG(LL_INFO, LM_MAIN, "");

	std::locale::global(std::locale("C"));
	std::cout.imbue(std::locale());
	std::cout << std::boolalpha;
	std::cin.imbue(std::locale());
	std::cerr.imbue(std::locale());

	std::signal(SIGINT, [](int signal) { std::exit(1); });

	Clock::Initialise();
	SetGlobalLogLevel(LL_DEBUG);

	std::optional<Config> config = LoadConfig(argc, argv, appType);
	if (!config)
	{
		LOG(LL_ERROR, LM_MAIN, "error loading config file, stopping");
		return std::nullopt;
	}
	SetGlobalLogLevel(config->mLogLevel);
	LOG(LL_INFO, LM_MAIN, "setting log-level to: %s", LogLevelToString(config->mLogLevel).c_str());

	if (config->mRunHot == RunHot::Yes)
	{
		LOG(LL_INFO, LM_MAIN, "running hot");
		SetAffinity(config->mCoreAffinity);
	}

	config->mAppName = appName;

	return AppContext{.mConfig=*config, .mEventLoop=EventLoop()};
}

int RunApp(EventLoop& eventLoop, App& app)
{
	try
	{
		app.Start();
		eventLoop.Run(app.GetConfig().mRunHot);
	}
	catch (const std::exception& e)
	{
		LOG(LL_ERROR, LM_MAIN, "program terminated with uncaught exception: %s", e.what());
		return 1;
	}
	return 0;
}

}