#include "app_audio_receiver.h"
#include <core/app.h>

using namespace DmxEnttecNode;

int main(int argc, char *argv[])
{
	std::optional<AppContext> context = SetupApp(argc, argv, "audio_receiver", AppType::AudioReceiver);
	if (!context)
	{
		return 1;
	}
	AppAudioReceiver app = AppAudioReceiver(context->mConfig, context->mEventLoop);
	RunApp(context->mEventLoop, app);
}