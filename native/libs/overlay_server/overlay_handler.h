#pragma once

#include <proto/overlay_proto.h>
#include <core/types.h>

namespace DmxEnttecNode {

class IOverlayHandler
{
public:
	virtual ~IOverlayHandler() = default;

	virtual void OnOverlayMessage(const OverlayIdl::OverlayMessage&) = 0;
};

}