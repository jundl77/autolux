add_library(radius_bus_lib bus_protocol.cc)
target_link_libraries(radius_bus_lib PUBLIC radius_core_lib radius_wire_lib nlohmann_json::nlohmann_json)
target_compile_options(radius_bus_lib PRIVATE ${cxx_compile_options})
target_include_directories(radius_bus_lib PUBLIC ..)
