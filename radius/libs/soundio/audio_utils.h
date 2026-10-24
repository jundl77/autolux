#pragma once

#include <soundio/soundio.h>
#include <string>

namespace Radius {

static SoundIoBackend DEFAULT_SOUNDIO_BACKEND = SoundIoBackend::SoundIoBackendWasapi;

SoundIo* CreateSoundIo(SoundIoBackend);

SoundIoDevice* GetAudioInputDevice(SoundIo*, const std::string& inputDeviceId, bool isRawDevice);
SoundIoDevice* GetAudioOutputDevice(SoundIo*, const std::string& outputDeviceId, bool isRawDevice);

} // namespace Radius
