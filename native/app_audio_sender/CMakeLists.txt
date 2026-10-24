set(TARGET_NAME app_audio_sender)

add_executable(${TARGET_NAME}
        app_audio_sender.cc
        app_audio_sender.h
        main.cc)

target_link_libraries(${TARGET_NAME}
        dmx_enttec_node_config_lib
        dmx_enttec_node_core_lib
        dmx_enttec_node_soundio_lib)

target_link_libraries(${TARGET_NAME})

install(TARGETS ${TARGET_NAME} DESTINATION ${CMAKE_BINARY_DIR}/${TARGET_NAME})

add_custom_command (TARGET ${TARGET_NAME} POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different
    ${CMAKE_BINARY_DIR}/x_libsoundio/soundio.dll ${CMAKE_BINARY_DIR}/${TARGET_NAME}
    )