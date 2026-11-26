/*
 * iMirror Remote — ESP32 firmware.
 *
 * Turns an ESP32 into a Bluetooth LE keyboard + mouse + media-keys device for iPhone.
 * The PC (iMirror app) sends commands over USB serial; the ESP32 forwards them to the
 * iPhone as standard HID reports. With AssistiveTouch enabled on the iPhone, the mouse
 * moves an on-screen pointer: click = tap, drag = swipe, wheel = scroll.
 *
 * Serial frame (PC -> ESP32):  0xA5 | cmd | len | payload[len] | chk
 *   chk = (cmd + len + sum(payload)) & 0xFF
 *
 *   0x01 MOVE      i16 dx, i16 dy, i8 wheel, i8 pan, u8 step   (little-endian)
 *   0x02 BUTTONS   u8 mask (bit0 L, bit1 R, bit2 M, bit3 Back, bit4 Fwd, bit5-7 extra)
 *   0x03 KEYS      u8 modifiers, u8 keys[6]  (HID usage codes)
 *   0x04 CONSUMER  u16 usage (0 = release)
 *   0x05 DELAY     u16 milliseconds (keeps ordering, e.g. long press)
 *   0x06 RELEASE   clear queue, release every key/button
 *   0x07 PING      -> replies "IMIRROR_FW ..." line
 *   0x08 INTERVAL  u8 ms between HID reports (1..50, default 8)
 *   0x09 UNPAIR    forget all bonded phones
 *   0x0A CLEARMOVE drop pending movement (keeps button/key state)
 *
 * Text lines (ESP32 -> PC): "IMIRROR_FW ...", "IMIRROR_EVT ...".
 *
 * Reliability rules:
 *  - commands are executed strictly in order (a click never overtakes the movement before it);
 *  - consecutive moves are merged, then split into steps of at most `step` counts per report;
 *  - every button/key/consumer state change is re-sent once ~25 ms later, so a lost BLE
 *    notification can never leave a finger "stuck" on the screen.
 */
#include <Arduino.h>
#include <NimBLEDevice.h>
#include <NimBLEHIDDevice.h>

#define FW_VERSION "1.0"
#define DEVICE_NAME "iMirror Remote"

#define RID_KEYBOARD 1
#define RID_MOUSE    2
#define RID_CONSUMER 3

static const uint8_t REPORT_MAP[] = {
    // ---------------- Keyboard (report id 1) ----------------
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01, 0x85, RID_KEYBOARD,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7, 0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08, 0x81, 0x02,             // modifiers
    0x95, 0x01, 0x75, 0x08, 0x81, 0x01,             // reserved
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,  // LEDs
    0x95, 0x01, 0x75, 0x03, 0x91, 0x01,             // LED padding
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x26, 0xFF, 0x00,
    0x05, 0x07, 0x19, 0x00, 0x2A, 0xFF, 0x00, 0x81, 0x00,  // 6 keys
    0xC0,
    // ---------------- Mouse (report id 2) ----------------
    0x05, 0x01, 0x09, 0x02, 0xA1, 0x01, 0x85, RID_MOUSE,
    0x09, 0x01, 0xA1, 0x00,
    0x05, 0x09, 0x19, 0x01, 0x29, 0x08, 0x15, 0x00, 0x25, 0x01,
    0x95, 0x08, 0x75, 0x01, 0x81, 0x02,             // 8 buttons
    0x05, 0x01, 0x09, 0x30, 0x09, 0x31, 0x09, 0x38,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x03, 0x81, 0x06,  // X, Y, wheel
    0x05, 0x0C, 0x0A, 0x38, 0x02,
    0x15, 0x81, 0x25, 0x7F, 0x75, 0x08, 0x95, 0x01, 0x81, 0x06,  // AC Pan
    0xC0, 0xC0,
    // ---------------- Consumer control (report id 3) ----------------
    0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, RID_CONSUMER,
    0x15, 0x00, 0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03,
    0x75, 0x10, 0x95, 0x01, 0x81, 0x00,
    0xC0,
};

// --------------------------------------------------------------------------------------
enum OpType : uint8_t { OP_MOVE, OP_BUTTONS, OP_KEYS, OP_CONSUMER, OP_DELAY };

struct Op {
    OpType type;
    uint8_t step;       // MOVE
    int32_t dx, dy;     // MOVE
    int32_t wheel, pan; // MOVE
    uint8_t buttons;    // BUTTONS
    uint8_t keys[8];    // KEYS (report layout: mod, 0, k1..k6)
    uint16_t value;     // CONSUMER usage / DELAY ms
};

static const int QUEUE_SIZE = 384;
static Op queue_[QUEUE_SIZE];
static int qHead = 0, qCount = 0;

static NimBLEServer* server = nullptr;
static NimBLEHIDDevice* hid = nullptr;
static NimBLECharacteristic* inKeyboard = nullptr;
static NimBLECharacteristic* inMouse = nullptr;
static NimBLECharacteristic* inConsumer = nullptr;
static NimBLECharacteristic* outKeyboard = nullptr;

static volatile bool connected = false;
static volatile bool connChanged = false;
static uint32_t intervalUs = 8000;
static uint32_t lastSendUs = 0;
static uint32_t delayStartMs = 0;
static bool delayRunning = false;

static uint8_t curButtons = 0;
static uint8_t curKeys[8] = {0};
static uint16_t curConsumer = 0;
static bool resendMouse = false, resendKeys = false, resendConsumer = false;
static uint32_t resendAtMs = 0;
static const uint32_t RESEND_DELAY_MS = 25;

// --------------------------------------------------------------------------------------
static inline Op& qAt(int i) { return queue_[(qHead + i) % QUEUE_SIZE]; }

static void qClear() { qHead = 0; qCount = 0; delayRunning = false; }

static void qPop() {
    qHead = (qHead + 1) % QUEUE_SIZE;
    qCount--;
}

static bool qPush(const Op& op) {
    // Merge consecutive moves with the same step size into the tail entry.
    if (op.type == OP_MOVE && qCount > 0) {
        Op& tail = qAt(qCount - 1);
        if (tail.type == OP_MOVE && tail.step == op.step) {
            tail.dx += op.dx; tail.dy += op.dy;
            tail.wheel += op.wheel; tail.pan += op.pan;
            return true;
        }
    }
    if (qCount >= QUEUE_SIZE) {
        Serial.println("IMIRROR_EVT queue_full");
        return false;
    }
    qAt(qCount) = op;
    qCount++;
    return true;
}

static inline int32_t clampStep(int32_t v, int32_t lim) {
    if (v > lim) return lim;
    if (v < -lim) return -lim;
    return v;
}

// --------------------------------------------------------------------------------------
static void sendMouse(int8_t dx, int8_t dy, int8_t wheel, int8_t pan) {
    uint8_t r[5] = {curButtons, (uint8_t)dx, (uint8_t)dy, (uint8_t)wheel, (uint8_t)pan};
    inMouse->setValue(r, sizeof(r));
    inMouse->notify();
}

static void sendKeys() {
    inKeyboard->setValue(curKeys, sizeof(curKeys));
    inKeyboard->notify();
}

static void sendConsumer() {
    uint8_t r[2] = {(uint8_t)(curConsumer & 0xFF), (uint8_t)(curConsumer >> 8)};
    inConsumer->setValue(r, sizeof(r));
    inConsumer->notify();
}

static void scheduleResend() { resendAtMs = millis() + RESEND_DELAY_MS; }

static void releaseAll() {
    qClear();
    curButtons = 0;
    memset(curKeys, 0, sizeof(curKeys));
    curConsumer = 0;
    if (connected) {
        sendMouse(0, 0, 0, 0);
        sendKeys();
        sendConsumer();
        resendMouse = resendKeys = resendConsumer = true;
        scheduleResend();
    }
}

// --------------------------------------------------------------------------------------
class ServerCallbacks : public NimBLEServerCallbacks {
    void onConnect(NimBLEServer* s, ble_gap_conn_desc* desc) override {
        // Apple accessory guidelines: 15 ms interval (min == max == 15 ms is allowed), no latency.
        s->updateConnParams(desc->conn_handle, 12, 12, 0, 400);
        connected = true;
        connChanged = true;
    }
    void onDisconnect(NimBLEServer* s, ble_gap_conn_desc* desc) override {
        connected = s->getConnectedCount() > 0;
        connChanged = true;
    }
};

class LedCallbacks : public NimBLECharacteristicCallbacks {
    void onWrite(NimBLECharacteristic* c) override {
        std::string v = c->getValue();
        if (!v.empty()) {
            Serial.printf("IMIRROR_EVT leds=%u\n", (unsigned)(uint8_t)v[0]);
        }
    }
};

static void startBle() {
    NimBLEDevice::init(DEVICE_NAME);
    NimBLEDevice::setPower(ESP_PWR_LVL_P9);
    NimBLEDevice::setSecurityAuth(true, false, true);  // bonding, no MITM, secure connections
    NimBLEDevice::setSecurityIOCap(BLE_HS_IO_NO_INPUT_OUTPUT);

    server = NimBLEDevice::createServer();
    server->setCallbacks(new ServerCallbacks());
    server->advertiseOnDisconnect(true);

    hid = new NimBLEHIDDevice(server);
    inKeyboard = hid->inputReport(RID_KEYBOARD);
    outKeyboard = hid->outputReport(RID_KEYBOARD);
    outKeyboard->setCallbacks(new LedCallbacks());
    inMouse = hid->inputReport(RID_MOUSE);
    inConsumer = hid->inputReport(RID_CONSUMER);

    hid->manufacturer()->setValue("iMirror");
    hid->pnp(0x02, 0x05AC, 0x820A, 0x0210);
    hid->hidInfo(0x00, 0x01);
    hid->reportMap((uint8_t*)REPORT_MAP, sizeof(REPORT_MAP));
    hid->startServices();
    hid->setBatteryLevel(100);

    NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
    adv->setAppearance(0x03C0);  // generic HID
    adv->addServiceUUID(hid->hidService()->getUUID());
    adv->setScanResponse(true);
    adv->start();
}

// --------------------------------------------------------------------------------------
static void printInfo() {
    Serial.printf("IMIRROR_FW v=%s chip=%s connected=%d name=\"%s\" interval_ms=%u\n",
                  FW_VERSION, ESP.getChipModel(), connected ? 1 : 0, DEVICE_NAME,
                  (unsigned)(intervalUs / 1000));
}

static int16_t rd16(const uint8_t* p) { return (int16_t)(p[0] | (p[1] << 8)); }

static void handleFrame(uint8_t cmd, const uint8_t* p, uint8_t len) {
    Op op;
    memset(&op, 0, sizeof(op));
    switch (cmd) {
    case 0x01:
        if (len < 7) return;
        op.type = OP_MOVE;
        op.dx = rd16(p);
        op.dy = rd16(p + 2);
        op.wheel = (int8_t)p[4];
        op.pan = (int8_t)p[5];
        op.step = p[6] == 0 ? 127 : (p[6] > 127 ? 127 : p[6]);
        if (connected) qPush(op);
        break;
    case 0x02:
        if (len < 1) return;
        op.type = OP_BUTTONS;
        op.buttons = p[0];
        if (connected) qPush(op);
        break;
    case 0x03:
        if (len < 7) return;
        op.type = OP_KEYS;
        op.keys[0] = p[0];
        op.keys[1] = 0;
        memcpy(op.keys + 2, p + 1, 6);
        if (connected) qPush(op);
        break;
    case 0x04:
        if (len < 2) return;
        op.type = OP_CONSUMER;
        op.value = (uint16_t)(p[0] | (p[1] << 8));
        if (connected) qPush(op);
        break;
    case 0x05:
        if (len < 2) return;
        op.type = OP_DELAY;
        op.value = (uint16_t)(p[0] | (p[1] << 8));
        if (connected) qPush(op);
        break;
    case 0x06:
        releaseAll();
        break;
    case 0x07:
        printInfo();
        break;
    case 0x08:
        if (len < 1) return;
        intervalUs = (uint32_t)constrain(p[0], 1, 50) * 1000;
        printInfo();
        break;
    case 0x09: {
        releaseAll();
        NimBLEDevice::deleteAllBonds();
        std::vector<uint16_t> peers = server->getPeerDevices();
        for (uint16_t h : peers) server->disconnect(h);
        Serial.println("IMIRROR_EVT unpaired");
        break;
    }
    case 0x0A: {
        // Remove pending movement, keep ordering of everything else.
        int n = qCount;
        Op tmp[QUEUE_SIZE > 64 ? 64 : QUEUE_SIZE];
        int kept = 0;
        for (int i = 0; i < n && kept < 64; i++) {
            if (qAt(i).type != OP_MOVE) tmp[kept++] = qAt(i);
        }
        qClear();
        for (int i = 0; i < kept; i++) qPush(tmp[i]);
        break;
    }
    default:
        break;
    }
}

// Serial frame parser ------------------------------------------------------------------
static uint8_t rxState = 0, rxCmd = 0, rxLen = 0, rxPos = 0;
static uint8_t rxBuf[32];

static void pollSerial() {
    while (Serial.available() > 0) {
        uint8_t b = (uint8_t)Serial.read();
        switch (rxState) {
        case 0: if (b == 0xA5) rxState = 1; break;
        case 1: rxCmd = b; rxState = 2; break;
        case 2:
            rxLen = b; rxPos = 0;
            if (rxLen > sizeof(rxBuf)) { rxState = 0; break; }
            rxState = rxLen ? 3 : 4;
            break;
        case 3:
            rxBuf[rxPos++] = b;
            if (rxPos >= rxLen) rxState = 4;
            break;
        case 4: {
            uint8_t sum = rxCmd + rxLen;
            for (uint8_t i = 0; i < rxLen; i++) sum += rxBuf[i];
            if (sum == b) handleFrame(rxCmd, rxBuf, rxLen);
            else Serial.println("IMIRROR_EVT bad_checksum");
            rxState = 0;
            break;
        }
        }
    }
}

// HID scheduler ------------------------------------------------------------------------
static void pump() {
    if (!connected) return;
    uint32_t nowUs = micros();
    if ((uint32_t)(nowUs - lastSendUs) < intervalUs) return;

    // Repeat the latest state once ~25 ms after a change (protects against a lost
    // notification). Runs even while the queue waits on a DELAY (long press).
    if ((resendMouse || resendKeys || resendConsumer) && (int32_t)(millis() - resendAtMs) >= 0) {
        if (resendMouse) { sendMouse(0, 0, 0, 0); resendMouse = false; }
        else if (resendKeys) { sendKeys(); resendKeys = false; }
        else { sendConsumer(); resendConsumer = false; }
        lastSendUs = nowUs;
        return;
    }

    while (qCount > 0) {
        Op& op = qAt(0);
        switch (op.type) {
        case OP_DELAY: {
            uint32_t nowMs = millis();
            if (!delayRunning) { delayRunning = true; delayStartMs = nowMs; }
            if ((uint32_t)(nowMs - delayStartMs) < op.value) return;
            delayRunning = false;
            qPop();
            continue;   // a finished delay costs no report slot
        }
        case OP_MOVE: {
            int32_t sx = clampStep(op.dx, op.step);
            int32_t sy = clampStep(op.dy, op.step);
            int32_t sw = clampStep(op.wheel, 127);
            int32_t sp = clampStep(op.pan, 127);
            op.dx -= sx; op.dy -= sy; op.wheel -= sw; op.pan -= sp;
            bool done = (op.dx == 0 && op.dy == 0 && op.wheel == 0 && op.pan == 0);
            if (done) qPop();
            if (sx || sy || sw || sp) {
                sendMouse((int8_t)sx, (int8_t)sy, (int8_t)sw, (int8_t)sp);
                lastSendUs = nowUs;
                return;
            }
            continue;
        }
        case OP_BUTTONS:
            curButtons = op.buttons;
            qPop();
            sendMouse(0, 0, 0, 0);
            resendMouse = true; scheduleResend();
            lastSendUs = nowUs;
            return;
        case OP_KEYS:
            memcpy(curKeys, op.keys, sizeof(curKeys));
            qPop();
            sendKeys();
            resendKeys = true; scheduleResend();
            lastSendUs = nowUs;
            return;
        case OP_CONSUMER:
            curConsumer = op.value;
            qPop();
            sendConsumer();
            resendConsumer = true; scheduleResend();
            lastSendUs = nowUs;
            return;
        }
    }

}

void setup() {
    Serial.setRxBufferSize(1024);
    Serial.begin(115200);
    startBle();
    delay(50);
    printInfo();
}

void loop() {
    pollSerial();
    if (connChanged) {
        connChanged = false;
        if (!connected) {
            qClear();
            curButtons = 0; memset(curKeys, 0, sizeof(curKeys)); curConsumer = 0;
            resendMouse = resendKeys = resendConsumer = false;
        }
        Serial.printf("IMIRROR_EVT %s\n", connected ? "connected" : "disconnected");
    }
    pump();
    delay(0);
}
