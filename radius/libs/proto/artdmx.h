#pragma once
#include <array>
#include <cstdint>
#include <cstring>

namespace Radius {

// Art-Net ArtDmx, protocol version 14. SoundSwitch supplies local unicast output.
inline bool DecodeArtDmx(const void* raw, size_t size, std::array<uint8_t, 512>& frame, uint16_t& universe)
{
	if (size < 18)
		return false;
	const auto* data = static_cast<const uint8_t*>(raw);
	if (std::memcmp(data, "Art-Net\0", 8) || data[8] != 0 || data[9] != 0x50 ||
	    (uint16_t(data[10]) * 256 + data[11]) < 14 || data[15] > 127)
		return false;
	const size_t length = size_t(data[16]) * 256 + data[17];
	if (length < 2 || length > 512 || length % 2 || size != 18 + length)
		return false;
	universe = uint16_t(data[15]) * 256 + data[14];
	frame.fill(0);
	std::memcpy(frame.data(), data + 18, length);
	return true;
}

} // namespace Radius
