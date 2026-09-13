cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(mixos_esp32s3)
# The three inherited codec patches are overlays, not an incomplete managed
# component. Applied to the exact registry release by CMake after resolution.
include(${CMAKE_CURRENT_LIST_DIR}/codec_overlay.cmake)
