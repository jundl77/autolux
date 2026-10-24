set(name radius_core_lib)

file(GLOB sources *.cc *.h)

add_library(${name} ${sources})

target_compile_options(${name} PRIVATE ${cxx_compile_options})
target_include_directories(${name} PUBLIC ..)
target_link_libraries(${name} PUBLIC winmm)
