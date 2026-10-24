#include "audio_utils.h"
#include <core/throw_if.h>
#include <string>

namespace DmxEnttecNode {

namespace {

SoundIoFormat PrioritizedFormats[] = {
	SoundIoFormatFloat32NE,
	SoundIoFormatFloat32FE,
	SoundIoFormatS32NE,
	SoundIoFormatS32FE,
	SoundIoFormatS24NE,
	SoundIoFormatS24FE,
	SoundIoFormatS16NE,
	SoundIoFormatS16FE,
	SoundIoFormatFloat64NE,
	SoundIoFormatFloat64FE,
	SoundIoFormatU32NE,
	SoundIoFormatU32FE,
	SoundIoFormatU24NE,
	SoundIoFormatU24FE,
	SoundIoFormatU16NE,
	SoundIoFormatU16FE,
	SoundIoFormatS8,
	SoundIoFormatU8,
	SoundIoFormatInvalid,
};

int PrioritizedSampleRates[] = {
	48000,
	44100,
	96000,
	24000,
	0,
};

void PrintChannelLayout(const LogModule& logModule, const SoundIoChannelLayout* layout)
{
	if (layout->name)
	{
		LOG(LL_INFO, logModule, "%s", layout->name);
	}
	else
	{
		LOG(LL_INFO, logModule, "%s", soundio_get_channel_name(layout->channels[0]));
		for (int i = 1; i < layout->channel_count; i += 1)
		{
			LOG(LL_INFO, logModule, "%s",  soundio_get_channel_name(layout->channels[i]));
		}
	}
}

void PrintDevice(const LogModule& logModule, SoundIoDevice *device, bool is_default, bool verbose)
{
	const char *default_str = is_default ? "(default)" : "";
	const char *raw_str = device->is_raw ? "(raw)" : "";

	LOG(LL_INFO, logModule, " %s%s, id='%s' %s", device->name, default_str, device->id, raw_str);
	if (!verbose)
	{
		return;
	}

	if (device->probe_error)
	{
		LOG(LL_INFO, logModule, "  probe error: %s", soundio_strerror(device->probe_error));
	}
	else
	{
		LOG(LL_INFO, logModule, "  channel layouts:");
		for (int i = 0; i < device->layout_count; i += 1)
		{
			LOG(LL_INFO, logModule, "    ");
			PrintChannelLayout(logModule, &device->layouts[i]);
			LOG(LL_INFO, logModule, "");
		}
		if (device->current_layout.channel_count > 0)
		{
			LOG(LL_INFO, logModule, "  current layout: ");
			PrintChannelLayout(logModule, &device->current_layout);
			LOG(LL_INFO, logModule, "");
		}

		LOG(LL_INFO, logModule, "  sample rates:");
		for (int i = 0; i < device->sample_rate_count; i += 1)
		{
			struct SoundIoSampleRateRange *range = &device->sample_rates[i];
			LOG(LL_INFO, logModule, "    %d - %d", range->min, range->max);

		}
		if (device->sample_rate_current)
			LOG(LL_INFO, logModule, "  current sample rate: %d", device->sample_rate_current);

		LOG(LL_INFO, logModule, "  formats: ");
		for (int i = 0; i < device->format_count; i += 1)
		{
			const char *comma = (i == device->format_count - 1) ? "" : ", ";
			LOG(LL_INFO, logModule, "%s%s", soundio_format_string(device->formats[i]), comma);
		}

		LOG(LL_INFO, logModule, "");
		if (device->current_format != SoundIoFormatInvalid)
			LOG(LL_INFO, logModule, "  current format: %s", soundio_format_string(device->current_format));

		LOG(LL_INFO, logModule, "  min software latency: %0.8f sec", device->software_latency_min);
		LOG(LL_INFO, logModule, "  max software latency: %0.8f sec", device->software_latency_max);
		if (device->software_latency_current != 0.0)
			LOG(LL_INFO, logModule, "  current software latency: %0.8f sec", device->software_latency_current);

	}
	LOG(LL_INFO, logModule, "");
}

}

SoundIo* CreateSoundIo(SoundIoBackend backend)
{
	SoundIo* soundIo = soundio_create();
	int err = (backend == SoundIoBackendNone) ?
			  soundio_connect(soundIo) : soundio_connect_backend(soundIo, backend);
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
			SoundIoDevice *device = soundio_get_input_device(soundio, i);
			if (device->is_raw == isRawDevice && strcmp(device->id, inputDeviceId.c_str()) == 0)
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
			if (device->is_raw == isRawDevice && strcmp(device->id, outputDeviceId.c_str()) == 0)
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

void LogSupportedDevices(const LogModule& logModule, SoundIo* soundio, bool verbose, bool logInputDevices, bool logOutputDevices)
{
	int outputCount = 0;
	int inputCount = 0;

	int default_output = soundio_default_output_device_index(soundio);
	int default_input = soundio_default_input_device_index(soundio);

	if (logInputDevices)
	{
		inputCount = soundio_input_device_count(soundio);
		LOG(LL_INFO, logModule, "--------Input Devices--------");
		for (int i = 0; i < inputCount; i += 1)
		{
			SoundIoDevice *device = soundio_get_input_device(soundio, i);
			PrintDevice(logModule, device, default_input == i, verbose);
			soundio_device_unref(device);
		}
	}

	if (logOutputDevices)
	{
		outputCount = soundio_output_device_count(soundio);
		LOG(LL_INFO, logModule, "--------Output Devices--------");
		for (int i = 0; i < outputCount; i += 1)
		{
			SoundIoDevice *device = soundio_get_output_device(soundio, i);
			PrintDevice(logModule, device, default_output == i, verbose);
			soundio_device_unref(device);
		}
	}

	LOG(LL_INFO, logModule, "%d devices found", inputCount + outputCount);
}

void LogSupportedFormats(const LogModule& logModule, SoundIoDevice* device)
{
	LOG(LL_INFO, logModule, "supported formats on '%s':", device->name);

	SoundIoFormat* fmt;
	bool hasValidFormat = false;
	for (fmt = PrioritizedFormats; *fmt != SoundIoFormatInvalid; fmt += 1)
	{
		if (soundio_device_supports_format(device, *fmt))
		{
			hasValidFormat = true;
			LOG(LL_INFO, logModule, "  format: %s", SoundIoFormatToString(*fmt).c_str());
		}
	}
	THROW_IF(!hasValidFormat, "incompatible sample formats with audio device");
}

void LogSupportedSampleRates(const LogModule& logModule, SoundIoDevice* device)
{
	LOG(LL_INFO, logModule, "supported sample rates on '%s':", device->name);

	int* sampleRatePtr;
	bool hasValidSampleRate = false;
	for (sampleRatePtr = PrioritizedSampleRates; *sampleRatePtr; sampleRatePtr += 1)
	{
		if (soundio_device_supports_sample_rate(device, *sampleRatePtr))
		{
			hasValidSampleRate = true;
			LOG(LL_INFO, logModule, "  sample rate: %d", *sampleRatePtr);
		}
	}
	THROW_IF(!hasValidSampleRate, "incompatible sample rates with audio device");
}

std::string SoundIoFormatToString(SoundIoFormat format)
{
	switch (format)
	{
		case SoundIoFormat::SoundIoFormatInvalid: return "SoundIoFormatInvalid";
		case SoundIoFormat::SoundIoFormatS8: return "SoundIoFormatS8";
		case SoundIoFormat::SoundIoFormatU8: return "SoundIoFormatU8";
		case SoundIoFormat::SoundIoFormatS16LE: return "SoundIoFormatS16LE";
		case SoundIoFormat::SoundIoFormatS16BE: return "SoundIoFormatS16BE";
		case SoundIoFormat::SoundIoFormatU16LE: return "SoundIoFormatU16LE";
		case SoundIoFormat::SoundIoFormatU16BE: return "SoundIoFormatU16BE";
		case SoundIoFormat::SoundIoFormatS24LE: return "SoundIoFormatS24LE";
		case SoundIoFormat::SoundIoFormatS24BE: return "SoundIoFormatS24BE";
		case SoundIoFormat::SoundIoFormatU24LE: return "SoundIoFormatU24LE";
		case SoundIoFormat::SoundIoFormatU24BE: return "SoundIoFormatU24BE";
		case SoundIoFormat::SoundIoFormatS32LE: return "SoundIoFormatS32LE";
		case SoundIoFormat::SoundIoFormatS32BE: return "SoundIoFormatS32BE";
		case SoundIoFormat::SoundIoFormatU32LE: return "SoundIoFormatU32LE";
		case SoundIoFormat::SoundIoFormatU32BE: return "SoundIoFormatU32BE";
		case SoundIoFormat::SoundIoFormatFloat32LE: return "SoundIoFormatFloat32LE";
		case SoundIoFormat::SoundIoFormatFloat32BE: return "SoundIoFormatFloat32BE";
		case SoundIoFormat::SoundIoFormatFloat64LE: return "SoundIoFormatFloat64LE";
		case SoundIoFormat::SoundIoFormatFloat64BE: return "SoundIoFormatFloat64BE";
	}
	THROW_IF(true, "unknown SoundIoFormat");
}

SoundIoFormat SoundIoFormatFromString(const std::string& format)
{
	if (format == "SoundIoFormatInvalid") { return SoundIoFormatInvalid; }
	if (format == "SoundIoFormatS8") { return SoundIoFormatS8; }
	if (format == "SoundIoFormatU8") { return SoundIoFormatU8; }
	if (format == "SoundIoFormatS16LE") { return SoundIoFormatS16LE; }
	if (format == "SoundIoFormatS16BE") { return SoundIoFormatS16BE; }
	if (format == "SoundIoFormatU16LE") { return SoundIoFormatU16LE; }
	if (format == "SoundIoFormatU16BE") { return SoundIoFormatU16BE; }
	if (format == "SoundIoFormatS24LE") { return SoundIoFormatS24LE; }
	if (format == "SoundIoFormatS24BE") { return SoundIoFormatS24BE; }
	if (format == "SoundIoFormatU24LE") { return SoundIoFormatU24LE; }
	if (format == "SoundIoFormatU24BE") { return SoundIoFormatU24BE; }
	if (format == "SoundIoFormatS32LE") { return SoundIoFormatS32LE; }
	if (format == "SoundIoFormatS32BE") { return SoundIoFormatS32BE; }
	if (format == "SoundIoFormatU32LE") { return SoundIoFormatU32LE; }
	if (format == "SoundIoFormatU32BE") { return SoundIoFormatU32BE; }
	if (format == "SoundIoFormatFloat32LE") { return SoundIoFormatFloat32LE; }
	if (format == "SoundIoFormatFloat32BE") { return SoundIoFormatFloat32BE; }
	if (format == "SoundIoFormatFloat64LE") { return SoundIoFormatFloat64LE; }
	if (format == "SoundIoFormatFloat64BE") { return SoundIoFormatFloat64BE; }
	THROW_IF(true, "string does not match any SoundIoFormat");
}

}