#pragma once

#include "overlay_proto.h"
#include <cstring>

namespace Radius {

inline bool DecodeOverlayPacket(const void* data, size_t len, OverlayIdl::OverlayMessage& out)
{
	if (len != sizeof(out))
		return false;
	std::memcpy(&out, data, sizeof(out));
	if (out.mProtocolId != OVERLAY_PROTOCOL_ID || out.mNumOverlays > 100)
		return false;
	for (size_t i = 0; i < out.mNumOverlays; ++i)
	{
		const auto& overlay = out.mOverlays[i];
		if (overlay.mStart > DmxFrameSize || overlay.mLength > DmxFrameSize - overlay.mStart)
			return false;
	}
	return true;
}

} // namespace Radius
