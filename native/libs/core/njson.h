#include <assert.h>
#define JSON_ASSERT(x) { assert(x); }
#include <nlohmann/json.hpp>

using njson = nlohmann::json;