#pragma once
#include <bus/bus_protocol.h>
#include <cstdint>
#include <core/clock.h>

namespace Radius {

inline bool IsInputStale(uint64_t nowMilliseconds, uint64_t receivedAtMilliseconds, uint64_t timeoutMilliseconds)
{
	return !receivedAtMilliseconds ||
	       (nowMilliseconds > receivedAtMilliseconds && nowMilliseconds - receivedAtMilliseconds > timeoutMilliseconds);
}

class LinkState
{
	uint64_t mStarted, mSent = 0, mReceived = 0;
	bool mHasSent = false, mHasReceived = false;

public:
	explicit LinkState(uint64_t now)
	    : mStarted(now)
	{
	}

	bool Due(uint64_t now) const
	{
		return !mHasSent || now - mSent >= Bus::HeartbeatMilliseconds;
	}

	void Sent(uint64_t now)
	{
		mHasSent = true;
		mSent = now;
	}

	void Received(uint64_t now)
	{
		mHasReceived = true;
		mReceived = now;
	}

	bool Up(uint64_t now) const
	{
		return mHasReceived && now - mReceived < Bus::TimeoutMilliseconds;
	}

	bool Expired(uint64_t now) const
	{
		return now - (mHasReceived ? mReceived : mStarted) >= Bus::TimeoutMilliseconds;
	}

	double Age(uint64_t now) const
	{
		return (now - (mHasReceived ? mReceived : mStarted)) / double(MillisecondsPerSecond);
	}
};

} // namespace Radius
