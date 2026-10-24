set(generated_dir ${CMAKE_CURRENT_BINARY_DIR}/generated)
file(MAKE_DIRECTORY ${generated_dir})
add_custom_command(
    OUTPUT ${generated_dir}/radius.pb.cc ${generated_dir}/radius.pb.h
    COMMAND protobuf::protoc --proto_path=${CMAKE_CURRENT_SOURCE_DIR}
            --cpp_out=${generated_dir} ${CMAKE_CURRENT_SOURCE_DIR}/radius.proto
    DEPENDS protobuf::protoc ${CMAKE_CURRENT_SOURCE_DIR}/radius.proto
    VERBATIM)
add_library(radius_wire_lib ${generated_dir}/radius.pb.cc)
target_include_directories(radius_wire_lib PUBLIC ${generated_dir})
target_link_libraries(radius_wire_lib PUBLIC protobuf::libprotobuf-lite utf8_validity)

add_library(radius_proto_lib radius.cc overlay_proto.cc)
target_link_libraries(radius_proto_lib PUBLIC radius_core_lib radius_wire_lib nlohmann_json::nlohmann_json)
target_compile_options(radius_proto_lib PRIVATE ${cxx_compile_options})
target_include_directories(radius_proto_lib PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/../..)
