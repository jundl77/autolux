#pragma once
#include "overlay_proto.h"
#include <array>
#include <algorithm>
#include <cstring>

namespace Radius {

class DmxState
{
	std::array<uint8_t, 512> mBase{};
	OverlayIdl::OverlayMessage mOverlay{};

public:
	void Base(const DmxFrame& frame)
	{
		std::memcpy(mBase.data(), frame.data(), 512);
	}

	void Overlay(const OverlayIdl::OverlayMessage& overlay)
	{
		mOverlay = overlay;
	}

	std::array<uint8_t, 512> Compose() const
	{
		auto frame = mBase;
		for (size_t i = 0; i < (std::min)(size_t(mOverlay.mNumOverlays), size_t(100)); ++i)
		{
			const auto start = mOverlay.mOverlays[i].mStart, length = mOverlay.mOverlays[i].mLength;
			if (size_t(start) + length <= frame.size())
				std::memcpy(frame.data() + start, mOverlay.mDmxData + start, length);
		}
		return frame;
	}
};

} // namespace Radius
