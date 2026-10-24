set(name dmx_enttec_node_enttec_lib)

file(GLOB libenttec_sources libenttec/*.cc libenttec/*.h)
file(GLOB sources *.cc *.h)

add_library(${name} ${sources} ${libenttec_sources})
target_link_libraries(${name}
        dmx_enttec_node_core_lib
        dmx_enttec_node_config_lib
        dmx_enttec_node_artnet_lib
        dmx_enttec_node_proto_lib
        dmx_enttec_node_overlay_server_lib
        )

target_link_libraries(${name} ${FTD2XX_LIBRARY})

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ..)