set(name dmx_enttec_node_network_lib)

file(GLOB sources *.cc *.h)

add_library(${name} ${sources})
target_link_libraries(${name}
        dmx_enttec_node_core_lib
        )

IF (WIN32)
    target_link_libraries(${name} ws2_32 iphlpapi)
ENDIF()

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ..)