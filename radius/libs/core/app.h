#pragma once

#include "event_loop.h"
#include <config/config.h>
#include <string>
#include <optional>
#include <iostream>

namespace Radius {

class App
{
public:
	virtual ~App() = default;
	virtual void Start() = 0;

	virtual bool IsReady() const
	{
		return true;
	}

	virtual const Config& GetConfig() const = 0;
};

struct AppContext
{
	Config mConfig;
	EventLoop mEventLoop;
};

std::optional<AppContext> SetupApp(int argc, char* argv[], const std::string& appName, AppType appType);
int RunApp(EventLoop&, App&);

template <class Application> int AppMain(int argc, char* argv[], const std::string& name, AppType type)
{
	try
	{
		if (argc == 3 && std::string(argv[1]) == "--check-config")
			return Config::FromFile(argv[2], type) ? 0 : 1;
		auto context = SetupApp(argc, argv, name, type);
		if (!context)
			return 1;
		Application app(context->mConfig, context->mEventLoop);
		return RunApp(context->mEventLoop, app);
	}
	catch (const std::exception& error)
	{
		std::cerr << error.what() << '\n';
		return 1;
	}
}

} // namespace Radius
