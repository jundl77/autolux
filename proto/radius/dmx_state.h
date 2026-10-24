#pragma once
#include <proto/radius/overlay_proto.h>
#include <array>
#include <algorithm>
#include <cstring>

namespace Radius {

class DmxState
{
	std::array<uint8_t, DmxFrameSize> mBase{};
	OverlayIdl::OverlayMessage mOverlay{};

public:
	void Base(const DmxFrame& frame)
	{
		std::memcpy(mBase.data(), frame.data(), DmxFrameSize);
	}

	void Overlay(const OverlayIdl::OverlayMessage& overlay)
	{
		mOverlay = overlay;
	}

	std::array<uint8_t, DmxFrameSize> Compose() const
	{
		auto frame = mBase;
		for (size_t i = 0; i < (std::min)(size_t(mOverlay.mNumOverlays), std::size(mOverlay.mOverlays)); ++i)
		{
			const auto start = mOverlay.mOverlays[i].mStart, length = mOverlay.mOverlays[i].mLength;
			if (size_t(start) + length <= frame.size())
				std::memcpy(frame.data() + start, mOverlay.mDmxData + start, length);
		}
		return frame;
	}
};

} // namespace Radius
