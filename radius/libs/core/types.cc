#include "types.h"
#include <stdexcept>

namespace Radius {

std::string ToString(const DmxFrame& frame)
{
	return FormatByteValuesAsDecimal<DmxFrameSize>(frame);
}

} // namespace Radius
