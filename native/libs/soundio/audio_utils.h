#pragma once

#include <core/logger.h>
#include <soundio/soundio.h>
#include <string>

namespace DmxEnttecNode {

static SoundIoBackend DEFAULT_SOUNDIO_BACKEND = SoundIoBackend::SoundIoBackendWasapi;

SoundIo* CreateSoundIo(SoundIoBackend);

SoundIoDevice* GetAudioInputDevice(SoundIo*, const std::string& inputDeviceId, bool isRawDevice);
SoundIoDevice* GetAudioOutputDevice(SoundIo*, const std::string& outputDeviceId, bool isRawDevice);

void LogSupportedDevices(const LogModule&, SoundIo*, bool verbose, bool logInputDevices, bool logOutputDevices);
void LogSupportedFormats(const LogModule&, SoundIoDevice*);
void LogSupportedSampleRates(const LogModule&, SoundIoDevice*);

std::string SoundIoFormatToString(SoundIoFormat);
SoundIoFormat SoundIoFormatFromString(const std::string&);

}