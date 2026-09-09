cmake_minimum_required(VERSION 3.24)
project(pal_shell_native VERSION 0.1.0 LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT UNIX)
  message(FATAL_ERROR "pal-shell-native supports Linux and macOS only")
endif()
find_package(Python3 REQUIRED COMPONENTS Interpreter Development.Module)
find_package(Threads REQUIRED)
find_package(Git REQUIRED)
option(BUILD_TESTING "Build the embedded-Python GIL lifetime regression" OFF)
include(FetchContent)
# Archive checksums lock the dependency contents as well as their revisions.
# SOURCE_SUBDIR keeps header-only dependencies' own projects out of this build.
FetchContent_Declare(ff
  URL https://codeload.github.com/OtakuNathan/flux_foundry/tar.gz/781375ab57884cbccb84b9d91133c2b2a22a95e9
  URL_HASH SHA256=7cf39e7047ed7b2067d0ce571cc67740a1108b4084405a687fb5e06947aa32eb
  SOURCE_SUBDIR _headers_only)
FetchContent_Declare(bridge
  URL https://codeload.github.com/OtakuNathan/dynabridge/tar.gz/0762d81175a6f3172b4a142b026d8392150fca6f
  URL_HASH SHA256=955f132e17bc648000e086f069dbc983ef9172695145b35e667cb52aca7ef9c9
  SOURCE_SUBDIR _headers_only
  PATCH_COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/dependencies/apply_patch.py"
    "${GIT_EXECUTABLE}" <SOURCE_DIR> "${CMAKE_CURRENT_SOURCE_DIR}/dependencies/bridge-gil-lifetime.patch")
set(LIBUV_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LIBUV_BUILD_BENCH OFF CACHE BOOL "" FORCE)
set(LIBUV_BUILD_SHARED OFF CACHE BOOL "" FORCE)
FetchContent_Declare(libuv
  URL https://codeload.github.com/libuv/libuv/tar.gz/refs/tags/v1.50.0
  URL_HASH SHA256=b1ec56444ee3f1e10c8bd3eed16ba47016ed0b94fe42137435aaf2e0bd574579)
FetchContent_MakeAvailable(ff bridge libuv)
Python3_add_library(_pal_shell_runtime MODULE WITH_SOABI module.cpp runtime.cpp process_posix.cpp)
target_include_directories(_pal_shell_runtime PRIVATE . "${ff_SOURCE_DIR}" "${bridge_SOURCE_DIR}")
target_link_libraries(_pal_shell_runtime PRIVATE uv_a Threads::Threads ${CMAKE_DL_LIBS})
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
  target_link_libraries(_pal_shell_runtime PRIVATE util rt)
  target_link_options(_pal_shell_runtime PRIVATE "LINKER:--exclude-libs,ALL")
endif()
target_compile_options(_pal_shell_runtime PRIVATE -Wall -Wextra -Wpedantic)
set_target_properties(_pal_shell_runtime PROPERTIES CXX_VISIBILITY_PRESET hidden VISIBILITY_INLINES_HIDDEN ON)
install(TARGETS _pal_shell_runtime LIBRARY DESTINATION . COMPONENT native)
foreach(dep IN ITEMS ff bridge libuv)
  install(FILES "${${dep}_SOURCE_DIR}/LICENSE" DESTINATION pal_shell_native_licenses
    RENAME "${dep}-LICENSE" COMPONENT native)
endforeach()
install(FILES THIRD_PARTY.md DESTINATION pal_shell_native_licenses COMPONENT native)
if(BUILD_TESTING)
  enable_testing()
  find_package(Python3 REQUIRED COMPONENTS Development.Embed)
  add_test(NAME backend COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/test_backend.py")
  set_tests_properties(backend PROPERTIES ENVIRONMENT "PYTHONPATH=$<TARGET_FILE_DIR:_pal_shell_runtime>" TIMEOUT 60)
  add_executable(shell_gil_lifetime "${bridge_SOURCE_DIR}/tests/python_executor_lifetime.cpp")
  target_include_directories(shell_gil_lifetime PRIVATE "${bridge_SOURCE_DIR}" "${ff_SOURCE_DIR}")
  target_link_libraries(shell_gil_lifetime PRIVATE Python3::Python Threads::Threads)
  add_test(NAME shell_gil_lifetime COMMAND shell_gil_lifetime)
  set_tests_properties(shell_gil_lifetime PROPERTIES TIMEOUT 20)
endif()
