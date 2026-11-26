#pragma once
#include "Arduino.h"
struct ble_gap_conn_desc { uint16_t conn_handle; };
struct NimBLEUUID {};
class NimBLECharacteristic;
class NimBLECharacteristicCallbacks { public: virtual ~NimBLECharacteristicCallbacks() {} virtual void onWrite(NimBLECharacteristic*) {} };
class NimBLECharacteristic {
 public:
  int id; std::vector<uint8_t> v;
  explicit NimBLECharacteristic(int i) : id(i) {}
  void setValue(const uint8_t* d, size_t n) { v.assign(d, d + n); }
  void setValue(const char*) {}
  void notify();
  std::string getValue() { return std::string(v.begin(), v.end()); }
  void setCallbacks(NimBLECharacteristicCallbacks*) {}
};
class NimBLEServer;
class NimBLEServerCallbacks { public: virtual ~NimBLEServerCallbacks() {} virtual void onConnect(NimBLEServer*, ble_gap_conn_desc*) {} virtual void onDisconnect(NimBLEServer*, ble_gap_conn_desc*) {} };
class NimBLEServer {
 public:
  NimBLEServerCallbacks* cb = nullptr; size_t count = 0;
  void setCallbacks(NimBLEServerCallbacks* c) { cb = c; }
  void advertiseOnDisconnect(bool) {}
  void updateConnParams(uint16_t, int, int, int, int) {}
  size_t getConnectedCount() { return count; }
  std::vector<uint16_t> getPeerDevices() { return {}; }
  int disconnect(uint16_t) { return 0; }
};
struct NimBLEService { NimBLEUUID getUUID() { return {}; } };
struct NimBLEAdvertising { void setAppearance(int) {} void addServiceUUID(NimBLEUUID) {} void setScanResponse(bool) {} bool start() { return true; } };
struct NimBLEDevice {
  static NimBLEServer* srv;
  static void init(const std::string&) {}
  static void setPower(int) {}
  static void setSecurityAuth(bool, bool, bool) {}
  static void setSecurityIOCap(int) {}
  static NimBLEServer* createServer() { return srv = new NimBLEServer(); }
  static NimBLEAdvertising* getAdvertising() { static NimBLEAdvertising a; return &a; }
  static void deleteAllBonds() {}
};
