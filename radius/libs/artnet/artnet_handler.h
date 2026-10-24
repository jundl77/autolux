#pragma once

#include <core/types.h>

namespace Radius {

class IArtnetHandler
{
public:
	virtual ~IArtnetHandler() = default;

	virtual void OnDmxMessage(const DmxFrame&, int universe) = 0;
};

} // namespace Radius
