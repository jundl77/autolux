#pragma once

#include <core/types.h>

namespace DmxEnttecNode {

class IArtnetHandler
{
public:
	virtual ~IArtnetHandler() = default;

	virtual void OnDmxMessage(const DmxFrame&, int universe) = 0;
};

}