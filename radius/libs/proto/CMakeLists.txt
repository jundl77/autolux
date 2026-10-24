set(name radius_proto_lib)

file(GLOB sources *.cc *.h)

add_library(${name} ${sources})
target_link_libraries(${name}
        radius_core_lib
        )

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ..)