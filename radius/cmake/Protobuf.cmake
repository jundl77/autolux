include(FetchContent)

# Build protoc and the lite runtime from the same pinned release. Dependency
# sources and generated messages stay in the ignored CMake build directory.
set(ABSL_PROPAGATE_CXX_STD ON CACHE BOOL "" FORCE)
set(ABSL_MSVC_STATIC_RUNTIME OFF CACHE BOOL "" FORCE)
set(protobuf_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(protobuf_BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(protobuf_MSVC_STATIC_RUNTIME OFF CACHE BOOL "" FORCE)
set(protobuf_BUILD_LIBUPB ON CACHE BOOL "" FORCE)
set(protobuf_WITH_ZLIB OFF CACHE BOOL "" FORCE)
set(protobuf_INSTALL OFF CACHE BOOL "" FORCE)
FetchContent_Declare(absl
    URL https://codeload.github.com/abseil/abseil-cpp/tar.gz/refs/tags/20250512.1
    URL_HASH SHA256=9b7a064305e9fd94d124ffa6cc358592eb42b5da588fb4e07d09254aa40086db)
FetchContent_Declare(protobuf
    URL https://codeload.github.com/protocolbuffers/protobuf/tar.gz/refs/tags/v33.5
    URL_HASH SHA256=440848dffa209beb8a04e41cc352762e44f8e91342b2a43aab6af9b30713c2f6)
FetchContent_MakeAvailable(absl protobuf)
