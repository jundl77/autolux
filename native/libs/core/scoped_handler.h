#pragma once

#include <functional>

namespace DmxEnttecNode {

class ScopedHandler
{
public:
	ScopedHandler() =default;

	template <typename Callable>
	explicit ScopedHandler(Callable&& callable) :
			mAtExit(std::forward<Callable>(callable))
	{}

	~ScopedHandler()
	{
		if (mAtExit)
		{
			mAtExit();
		}
	}

	ScopedHandler(const ScopedHandler&) =delete;
	ScopedHandler& operator=(ScopedHandler&) =delete;

	ScopedHandler(ScopedHandler&& other) :
			mAtExit(std::move(other.mAtExit))
	{
		other.mAtExit = nullptr;
	}

	ScopedHandler& operator=(ScopedHandler&& other)
	{
		if (mAtExit)
		{
			mAtExit();
			mAtExit = nullptr;
		}

		std::swap(mAtExit, other.mAtExit);
		return *this;
	}

private:
	std::function<void()> mAtExit;
};

}