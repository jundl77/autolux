#include "app.h"
#include <core/logger.h>
#include <core/clock.h>
#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>
#include <optional>
#include <filesystem>
#include <fstream>

#ifdef WIN32
#include <Windows.h>
#include <mmsystem.h>
#endif

namespace Radius {

namespace {

const LogModule LM_MAIN {"MAIN"};
volatile std::sig_atomic_t sStopRequested = 0;

std::optional<Config> LoadConfig(int argc, char *argv[], const std::string& name, AppType appType)
{
	std::string filePath = name + ".json";
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

	sStopRequested = 0;
	std::signal(SIGINT, [](int) { sStopRequested = 1; });
	std::signal(SIGTERM, [](int) { sStopRequested = 1; });

	Clock::Initialise();
	SetGlobalLogLevel(LL_DEBUG);

	std::optional<Config> config = LoadConfig(argc, argv, appName, appType);
	if (!config)
	{
		LOG(LL_ERROR, LM_MAIN, "error loading config file, stopping");
		return std::nullopt;
	}
	SetGlobalLogLevel(config->mLogLevel);
	LOG(LL_INFO, LM_MAIN, "setting log-level to: %s", LogLevelToString(config->mLogLevel).c_str());


	config->mAppName = appName;

	return AppContext{.mConfig=*config, .mEventLoop=EventLoop()};
}

int RunApp(EventLoop& eventLoop, App& app)
{
	try
	{
		struct TimerResolution { TimerResolution(){ timeBeginPeriod(1); } ~TimerResolution(){ timeEndPeriod(1); } } resolution;
		app.Start();
		const char* runtimeDir = std::getenv("AUTOLUX_RUNTIME_DIR");
		std::filesystem::path control = runtimeDir ? runtimeDir : "";
		if (runtimeDir) {
			std::ofstream ready(control / "ready");
			ready << "autolux-native-v1\n";
			ready.close();
			if (!ready) throw std::runtime_error("unable to signal native readiness");
		}
		auto stopPoll = eventLoop.AddTimer(100ms, [&]() {
			if (sStopRequested || (runtimeDir && std::filesystem::exists(control / "stop")))
				eventLoop.Stop();
		});
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
