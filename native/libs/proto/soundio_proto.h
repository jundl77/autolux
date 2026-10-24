#pragma once

#include <core/types.h>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <atomic>

namespace DmxEnttecNode::SoundIoIdl {

#define MAX_UDP_PACKET_SIZE 65535

#define SOUNDIO_PROTOCOL_ID 0x7798
#define SOUNDIO_MAX_DATA_SIZE 1024 * 20

#pragma pack(push, 1)

struct SoundIoData
{
	uint32_t mProtocolId {SOUNDIO_PROTOCOL_ID};
	uint32_t mSeqNum;
	uint32_t mSize;
	char mData[SOUNDIO_MAX_DATA_SIZE];
};
static_assert(sizeof(SoundIoData) < MAX_UDP_PACKET_SIZE);

#pragma pack(pop)

}