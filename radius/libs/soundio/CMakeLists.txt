set(name radius_soundio_lib)

file(GLOB sources *.cc *.h)

add_library(${name} ${sources})
target_link_libraries(${name}
        radius_core_lib
        radius_network_lib
        radius_bus_lib
        libsoundio_shared)

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_include_directories(${name} PUBLIC ../../x_libsoundio)
target_include_directories(${name} PUBLIC ..)