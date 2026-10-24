#include "overlay_proto.h"
#include <cstdint>
#include <stdexcept>
#include <string>
#include <atomic>

namespace DmxEnttecNode::OverlayIdl {

std::string ToString(std::span<DmxOverlay, 100> overlays)
{
#ifdef NDEBUG
	// don't compile this, because it's an expensive string computation
	return "";
#else
	std::string s = "\n";
	int index = -1;
	for (int i = 0; i < 5; i++)
	{
		for (int y = 0; y < 20; y++)
		{
			index += 1;
			if (index < 100)
			{
				const DmxOverlay& overlay = overlays[index];
				s += std::to_string(overlay.mStart) + "-" + std::to_string(overlay.mLength) + ", ";
			}
		}
		s += "\n";
	}
	return s;
#endif
}

std::string ToString(OverlayMessage& msg)
{
#ifdef NDEBUG
	// don't compile this, because it's an expensive string computation
	return "";
#else
	std::string s = "OverlayMessage[\n";
	s += "  protocol_id=" + std::to_string(msg.mProtocolId) + ", \n";
	s += "  universe=" + std::to_string(msg.mUniverse) + ", \n";
	s += "  num_overlays=" + std::to_string(msg.mNumOverlays) + ", \n";
	s += "  overlays=" + ToString(msg.mOverlays);
	s += "  dmx_data=" + DmxEnttecNode::ToString(msg.GetDmxFrame());
	s += "]";
	return s;
#endif
}

}