#include "app_audio_sender.h"
#include <core/app.h>

using namespace DmxEnttecNode;

int main(int argc, char *argv[])
{
	try
	{
		if (argc == 3 && std::string(argv[1]) == "--check-config")
		{
			char* checkArgs[] = {argv[0], argv[2]};
			return SetupApp(2, checkArgs, "audio_sender", AppType::AudioSender) ? 0 : 1;
		}
		std::optional<AppContext> context = SetupApp(argc, argv, "audio_sender", AppType::AudioSender);
		if (!context)
		{
			return 1;
		}
		AppAudioSender app = AppAudioSender(context->mConfig, context->mEventLoop);
		return RunApp(context->mEventLoop, app);
	}
	catch (const std::exception& error)
	{
		std::cerr << error.what() << '\n';
		return 1;
	}
}
