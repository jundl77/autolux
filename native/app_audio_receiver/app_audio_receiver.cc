#include "app_audio_receiver.h"
#include <core/logger.h>

#include <string>
#include <iostream>

namespace DmxEnttecNode {

static const LogModule LM_APP {"APP"};

AppAudioReceiver::AppAudioReceiver(const Config& config, EventLoop& loop)
	: mConfig(config)
	, mEventLoop(loop)
	, mAudioReceiver(mConfig, mEventLoop)
{
}

AppAudioReceiver::~AppAudioReceiver()
{
}

void AppAudioReceiver::Start()
{
	LOG(LL_INFO, LM_APP, "starting %s", mConfig.mAppName.c_str());
	mAudioReceiver.Start();
}

}
