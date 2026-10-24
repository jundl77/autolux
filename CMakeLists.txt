cmake_minimum_required(VERSION 3.5.1)

project(dmx_enttec_node LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX20_STANDARD_COMPILE_OPTION "-std:c++latest")
set(CMAKE_CXX20_EXTENSION_COMPILE_OPTION "-std:c++latest")

if (${CMAKE_CXX_COMPILER_ID} STREQUAL "GNU")
    set(cxx_compile_options -Wall -Wextra)
endif()

include(cmake/FindFTD2XX.cmake)

add_subdirectory(x_njson)
add_subdirectory(x_libsoundio)

add_subdirectory(libs)
add_subdirectory(app_dmx_usb_node)
add_subdirectory(app_audio_sender)
add_subdirectory(app_audio_receiver)

enable_testing()
add_executable(audio_delay_test tests/audio_delay_test.cc libs/soundio/audio_delay.cc)
target_include_directories(audio_delay_test PRIVATE libs)
add_test(NAME audio_delay COMMAND audio_delay_test)
