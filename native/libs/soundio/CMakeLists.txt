set(name dmx_enttec_node_soundio_lib)

file(GLOB sources *.cc *.h)

add_library(${name} ${sources})
target_link_libraries(${name}
        dmx_enttec_node_core_lib
        dmx_enttec_node_network_lib
        dmx_enttec_node_proto_lib
        libsoundio_shared)

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(${name} PUBLIC ../../x_libsoundio)
target_include_directories(${name} PUBLIC ..)