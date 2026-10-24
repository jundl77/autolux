#pragma once

#include <config/config.h>
#include <core/app.h>
#include <core/event_loop.h>
#include <soundio/audio_sender.h>

#include <string>
#include <memory>

namespace DmxEnttecNode {

class AppAudioSender : public App
{
public:
	AppAudioSender(const Config&, EventLoop& loop);
	~AppAudioSender() override;

	void Start() override;
	const Config& GetConfig() const override { return mConfig; };

private:
	const Config& mConfig;
	EventLoop& mEventLoop;
	AudioSender mAudioSender;
};

}