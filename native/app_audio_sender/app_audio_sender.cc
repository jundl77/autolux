#include "app_audio_sender.h"
#include <core/logger.h>

#include <string>
#include <iostream>

namespace DmxEnttecNode {

static const LogModule LM_APP {"APP"};

AppAudioSender::AppAudioSender(const Config& config, EventLoop& loop)
	: mConfig(config)
	, mEventLoop(loop)
	, mAudioSender(mConfig, mEventLoop)
{
}

AppAudioSender::~AppAudioSender()
{
}

void AppAudioSender::Start()
{
	LOG(LL_INFO, LM_APP, "starting %s", mConfig.mAppName.c_str());
	mAudioSender.Start();
}

}
