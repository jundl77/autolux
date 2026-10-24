add_executable(radius_tx radius_tx.cc main.cc)
target_link_libraries(radius_tx PRIVATE radius_config_lib radius_core_lib radius_soundio_lib radius_artnet_lib radius_overlay_server_lib radius_enttec_lib)
add_custom_command(TARGET radius_tx POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_if_different $<TARGET_FILE:libsoundio_shared> $<TARGET_FILE_DIR:radius_tx>
    COMMAND ${CMAKE_COMMAND} -E copy_if_different ${PROJECT_SOURCE_DIR}/libs/enttec/libenttec/ftd2xx/i386/ftd2xx.dll $<TARGET_FILE_DIR:radius_tx>)
