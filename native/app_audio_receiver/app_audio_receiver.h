#pragma once

#include <config/config.h>
#include <core/app.h>
#include <core/event_loop.h>
#include <soundio/audio_receiver.h>

#include <string>
#include <memory>

namespace DmxEnttecNode {

class AppAudioReceiver : public App
{
public:
	AppAudioReceiver(const Config&, EventLoop& loop);
	~AppAudioReceiver() override;

	void Start() override;
	const Config& GetConfig() const override { return mConfig; };

private:
	const Config& mConfig;
	EventLoop& mEventLoop;
	AudioReceiver mAudioReceiver;
};

}