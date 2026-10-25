cmake_minimum_required(VERSION 3.16)

project(radius LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX20_STANDARD_COMPILE_OPTION "-std:c++latest")
set(CMAKE_CXX20_EXTENSION_COMPILE_OPTION "-std:c++latest")

if (${CMAKE_CXX_COMPILER_ID} STREQUAL "GNU")
    set(cxx_compile_options -Wall -Wextra)
endif()

include(cmake/FindFTD2XX.cmake)
include(cmake/Protobuf.cmake)

add_subdirectory(x_njson)
add_subdirectory(x_libsoundio)

add_subdirectory(libs)
add_subdirectory(../proto/radius proto/radius)
add_subdirectory(radius_tx)
add_subdirectory(radius_rx)

enable_testing()
add_executable(audio_delay_test tests/audio_delay_test.cc libs/soundio/audio_delay.cc)
target_include_directories(audio_delay_test PRIVATE libs)
add_test(NAME audio_delay COMMAND audio_delay_test)
add_executable(transport_test tests/transport_test.cc tests/login_test.cc radius_rx/radius_rx.cc radius_tx/radius_tx.cc)
target_link_libraries(transport_test PRIVATE radius_config_lib radius_core_lib radius_soundio_lib radius_artnet_lib radius_enttec_lib)
add_custom_command(TARGET transport_test POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:libsoundio_shared> $<TARGET_FILE_DIR:transport_test>
    COMMAND ${CMAKE_COMMAND} -E copy_if_different ${PROJECT_SOURCE_DIR}/libs/enttec/libenttec/ftd2xx/i386/ftd2xx.dll $<TARGET_FILE_DIR:transport_test>)
add_test(NAME transport COMMAND transport_test)
add_executable(bus_test tests/bus_test.cc)
target_link_libraries(bus_test PRIVATE radius_bus_lib)
add_test(NAME bus COMMAND bus_test)
add_executable(usb_test tests/usb_test.cc libs/enttec/usb_client.cc libs/enttec/libenttec/usb_pro.cc)
target_include_directories(usb_test PRIVATE libs)
target_compile_definitions(usb_test PRIVATE FTD2XX_STATIC)
target_link_libraries(usb_test PRIVATE radius_core_lib)
add_test(NAME usb COMMAND usb_test)
