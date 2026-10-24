#include "app_dmx_usb_node.h"
#include <core/app.h>

using namespace DmxEnttecNode;

int main(int argc, char *argv[])
{
	std::optional<AppContext> context = SetupApp(argc, argv, "dmx_usb_node", AppType::DmxUsbNode);
	if (!context)
	{
		return 1;
	}
	AppDmxUsbNode app = AppDmxUsbNode(context->mConfig, context->mEventLoop);
	RunApp(context->mEventLoop, app);
}