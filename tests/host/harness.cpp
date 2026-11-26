// Host harness: runs the real firmware main.cpp against stdin frames, prints HID reports.
#include "Arduino.h"
#include "NimBLEDevice.h"
#ifdef _WIN32
#include <io.h>
#include <fcntl.h>
#endif
uint64_t g_now_us = 1000000;
FakeSerial Serial;
FakeESP ESP;
NimBLEServer* NimBLEDevice::srv = nullptr;
void NimBLECharacteristic::notify() {
  printf("%llu %d", (unsigned long long)(g_now_us / 1000), id);
  for (uint8_t b : v) printf(" %02x", b);
  printf("\n");
}
#include "../../firmware/src/main.cpp"
int main() {
#ifdef _WIN32
  _setmode(_fileno(stdin), _O_BINARY);
#endif
  int c;
  while ((c = getchar()) != EOF) Serial.in.push_back((uint8_t)c);
  setup();
  // simulate phone connecting
  NimBLEDevice::srv->count = 1;
  ble_gap_conn_desc d{1};
  NimBLEDevice::srv->cb->onConnect(NimBLEDevice::srv, &d);
  loop();
  for (int i = 0; i < 120000; i++) { g_now_us += 250; loop(); }
  return 0;
}
