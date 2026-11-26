#pragma once
#include "NimBLEDevice.h"
class NimBLEHIDDevice {
 public:
  explicit NimBLEHIDDevice(NimBLEServer*) {}
  NimBLECharacteristic* inputReport(uint8_t id) { return new NimBLECharacteristic(id); }
  NimBLECharacteristic* outputReport(uint8_t id) { return new NimBLECharacteristic(100 + id); }
  NimBLECharacteristic* manufacturer() { static NimBLECharacteristic c(0); return &c; }
  void pnp(int, int, int, int) {}
  void hidInfo(int, int) {}
  void reportMap(uint8_t*, uint16_t) {}
  void startServices() {}
  void setBatteryLevel(int) {}
  NimBLEService* hidService() { static NimBLEService s; return &s; }
};
