#pragma once

#include "strong_typedef.h"
#include <utility>
#include <functional>
#include <string>
#include <cstdint>
#include <span>

namespace DmxEnttecNode {

constexpr uint32_t DmxFrameSize = 512;

using DmxFrame = std::span<uint8_t, DmxFrameSize>;

std::string ToString(const DmxFrame&);

template<size_t T>
std::string HexToString(const std::span<uint8_t, T>& span)
{
#ifdef NDEBUG
	// don't compile this, because it's an expensive string computation
return "";
#else
	std::string s = "\n";
	int index = -1;
	for (int i = 0; i < 10; i++)
	{
		for (int y = 0; y < 55; y++)
		{
			index += 1;
			if (index < T)
			{
				s += std::to_string(span[index]) + " ";
			}
		}
		s += "\n";
	}
	return s;
#endif
}

}