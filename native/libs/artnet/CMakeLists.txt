set(name dmx_enttec_node_artnet_lib)

file(GLOB libartnet_sources libartnet/*.c libartnet/*.h)
file(GLOB sources *.cc *.h)

IF(!WIN32)
    add_compile_options(-Wunused-variable -Wunused-parameter)
ENDIF ()

add_library(${name} ${sources} ${libartnet_sources})
target_link_libraries(${name}
        dmx_enttec_node_core_lib
        dmx_enttec_node_config_lib
        dmx_enttec_node_enttec_lib
        )

IF(WIN32)
    target_link_libraries(${name} ws2_32 iphlpapi)
ENDIF ()

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ..)