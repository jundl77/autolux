set(name radius_enttec_lib)

file(GLOB libenttec_sources libenttec/*.cc libenttec/*.h)
file(GLOB sources *.cc *.h)

add_library(${name} ${sources} ${libenttec_sources})
target_link_libraries(${name}
        radius_core_lib
        radius_config_lib
        radius_bus_lib
        )

target_link_libraries(${name} ${FTD2XX_LIBRARY})

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ..)