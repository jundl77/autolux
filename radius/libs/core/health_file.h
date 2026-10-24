#pragma once
#include <core/njson.h>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <windows.h>
namespace Radius {
inline uint64_t SteadyMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
}
inline void WriteHealth(njson health) {
    const char* runtime=std::getenv("AUTOLUX_RUNTIME_DIR"); if(!runtime) return;
    health["timestamp"]=std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
    const auto path=std::filesystem::path(runtime)/"health.json", temp=std::filesystem::path(runtime)/"health.tmp";
    { std::ofstream output(temp); output<<health.dump(); if(!output) throw std::runtime_error("health file write failed"); }
    if(!MoveFileExW(temp.c_str(),path.c_str(),MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH))
        throw std::runtime_error("health file replacement failed");
}
}
