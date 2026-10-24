#pragma once

#include "event_loop.h"
#include <config/config.h>
#include <string>
#include <optional>

namespace DmxEnttecNode {

class App
{
public:
	virtual ~App() = default;
	virtual void Start() = 0;
	virtual const Config& GetConfig() const = 0;
};

struct AppContext
{
	Config mConfig;
	EventLoop mEventLoop;
};

std::optional<AppContext> SetupApp(int argc, char *argv[], const std::string& appName, AppType appType);
int RunApp(EventLoop&, App&);

}