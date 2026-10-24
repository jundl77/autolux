#pragma once

#include <stdexcept>

#define THROW_IF(cond, ...) \
	do \
	{ \
		if (cond) \
		{ \
			throw std::runtime_error(__VA_ARGS__); \
		} \
	} while(false)
