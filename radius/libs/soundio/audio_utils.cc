#include "audio_utils.h"
#include <core/throw_if.h>
#include <cstring>
#include <string>

namespace Radius {

SoundIo* CreateSoundIo(SoundIoBackend backend)
{
	SoundIo* soundIo = soundio_create();
	int err = (backend == SoundIoBackendNone) ? soundio_connect(soundIo) : soundio_connect_backend(soundIo, backend);
	THROW_IF(err, "error connecting: " + std::string(soundio_strerror(err)));
	soundio_flush_events(soundIo);
	return soundIo;
}

SoundIoDevice* GetAudioInputDevice(SoundIo* soundio, const std::string& inputDeviceId, bool isRawDevice)
{
	int defaultInDeviceIndex = soundio_default_input_device_index(soundio);
	THROW_IF(defaultInDeviceIndex < 0, "no input device found");

	int inDeviceIndex = defaultInDeviceIndex;
	if (!inputDeviceId.empty())
	{
		bool found = false;
		for (int i = 0; i < soundio_input_device_count(soundio); i += 1)
		{
			SoundIoDevice* device = soundio_get_input_device(soundio, i);
			if (device->is_raw == isRawDevice && std::strcmp(device->id, inputDeviceId.c_str()) == 0)
			{
				inDeviceIndex = i;
				found = true;
				soundio_device_unref(device);
				break;
			}
			soundio_device_unref(device);
		}
		THROW_IF(!found, "invalid input device id: " + inputDeviceId);
	}

	SoundIoDevice* inDevice = soundio_get_input_device(soundio, inDeviceIndex);
	THROW_IF(!inDevice, "could not get input device: out of memory");
	return inDevice;
}

SoundIoDevice* GetAudioOutputDevice(SoundIo* soundio, const std::string& outputDeviceId, bool isRawDevice)
{
	int defaultOutDeviceIndex = soundio_default_output_device_index(soundio);
	THROW_IF(defaultOutDeviceIndex < 0, "no output device found");

	int outDeviceIndex = defaultOutDeviceIndex;
	if (!outputDeviceId.empty())
	{
		bool found = false;
		for (int i = 0; i < soundio_output_device_count(soundio); i += 1)
		{
			SoundIoDevice* device = soundio_get_output_device(soundio, i);
			if (device->is_raw == isRawDevice && std::strcmp(device->id, outputDeviceId.c_str()) == 0)
			{
				outDeviceIndex = i;
				found = true;
				soundio_device_unref(device);
				break;
			}
			soundio_device_unref(device);
		}
		THROW_IF(!found, "invalid output device id: " + outputDeviceId);
	}

	SoundIoDevice* outDevice = soundio_get_output_device(soundio, outDeviceIndex);
	THROW_IF(!outDevice, "could not get output device: out of memory");
	return outDevice;
}

} // namespace Radius
