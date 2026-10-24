#pragma once

#include <core/types.h>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <atomic>

namespace DmxEnttecNode::OverlayIdl {

#define OVERLAY_PROTOCOL_ID 0x7799

#pragma pack(push, 1)

struct DmxOverlay
{
	uint16_t mStart;
	uint16_t mLength;
};

struct OverlayMessage
{
	uint32_t mProtocolId {OVERLAY_PROTOCOL_ID};
	uint8_t mUniverse;
	uint16_t mNumOverlays;
	DmxOverlay mOverlays[100];
	uint8_t mDmxData[512];

	DmxFrame GetDmxFrame()
	{
		return DmxFrame(mDmxData);
	}
};

#pragma pack(pop)

std::string ToString(std::span<DmxOverlay, 100> overlays);
std::string ToString(OverlayMessage& msg);

}