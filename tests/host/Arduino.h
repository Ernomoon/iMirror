#pragma once
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cstdarg>
#include <string>
#include <vector>
extern uint64_t g_now_us;
inline uint32_t micros() { return (uint32_t)g_now_us; }
inline uint32_t millis() { return (uint32_t)(g_now_us / 1000); }
inline void delay(int) {}
template <class T> T constrain(T v, T lo, T hi) { return v < lo ? lo : (v > hi ? hi : v); }
inline int constrain(uint8_t v, int lo, int hi) { return v < lo ? lo : (v > hi ? hi : v); }
struct FakeSerial {
  std::vector<uint8_t> in; size_t pos = 0;
  void begin(int) {}
  void setRxBufferSize(int) {}
  int available() { return (int)(in.size() - pos); }
  int read() { return pos < in.size() ? in[pos++] : -1; }
  void println(const char* s) { fprintf(stderr, "SER %s\n", s); }
  void printf(const char* f, ...) { va_list a; va_start(a, f); fprintf(stderr, "SER "); vfprintf(stderr, f, a); va_end(a); }
};
extern FakeSerial Serial;
struct FakeESP { const char* getChipModel() { return "HOST"; } };
extern FakeESP ESP;
#define ESP_PWR_LVL_P9 9
#define BLE_HS_IO_NO_INPUT_OUTPUT 3
