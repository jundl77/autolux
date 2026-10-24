#include "types.h"
#include <stdexcept>

namespace DmxEnttecNode
{

std::string ToString(const DmxFrame& frame)
{
	return HexToString<DmxFrameSize>(frame);
}

}