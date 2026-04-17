/*
 * Lock-in Charge Reader for usphere
 *
 * Reads the SR530 X output (±10V, scaled to ±2V by external ÷5 resistive
 * divider) on ADS1115 differential input (A0 − A1).
 *
 * Streams signed voltage over USB serial at ~200 Hz.
 *
 * Protocol
 * --------
 *   Output (ESP32 → PC):
 *     V:+0.00234\n          Voltage reading (after ÷5 correction → actual volts)
 *
 *   Input (PC → ESP32):
 *     ID?\n                 → responds  ID:USPHERE_LOCKIN_READER\n
 *     RATE:<hz>\n           → set output rate (10–500 Hz, default 200)
 *     AVG:<n>\n             → set averaging count (1–32, default 4)
 *
 * Hardware wiring
 * ---------------
 *   SR530 X OUT  ──┬── 80kΩ ──┬── ADS1115 A0 (AIN0)
 *                  │          │
 *                  │         20kΩ ── GND
 *                  │
 *                  └── (shield to GND)
 *
 *   ADS1115 A1 (AIN1) ── GND  (reference for differential)
 *
 *   The ÷5 divider maps ±10V → ±2V, well within ADS1115 PGA_4096 range (±4.096V).
 *   Code multiplies the raw reading by DIVIDER_RATIO to reconstruct the actual voltage.
 */

#include <Wire.h>
#include "Ads1115.h"

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

#define I2C_ADDR       (0x48u)     // ADS1115 default (ADDR → GND)
#define DIVIDER_RATIO  (5.0f)      // External resistive divider ratio

static Ads1115      adc(I2C_ADDR);
static unsigned int outputRateHz = 200;
static unsigned int avgCount     = 4;
static unsigned long periodUs    = 1000000UL / 200;

// ---------------------------------------------------------------------------
// Serial command parser
// ---------------------------------------------------------------------------

static void handleCommand(const String &cmd)
{
    if (cmd == "ID?") {
        Serial.println("ID:USPHERE_LOCKIN_READER");
    }
    else if (cmd.startsWith("RATE:")) {
        int rate = cmd.substring(5).toInt();
        if (rate >= 10 && rate <= 500) {
            outputRateHz = rate;
            periodUs     = 1000000UL / rate;
            Serial.print("OK:RATE:");
            Serial.println(rate);
        } else {
            Serial.println("ERR:RATE_RANGE");
        }
    }
    else if (cmd.startsWith("AVG:")) {
        int n = cmd.substring(4).toInt();
        if (n >= 1 && n <= 32) {
            avgCount = n;
            Serial.print("OK:AVG:");
            Serial.println(n);
        } else {
            Serial.println("ERR:AVG_RANGE");
        }
    }
    else {
        Serial.println("ERR:UNKNOWN_CMD");
    }
}

// ---------------------------------------------------------------------------
// Read one differential sample (A0 − A1)
// ---------------------------------------------------------------------------

static float readDifferential()
{
    adc.SetMux(ADS1115_MUX_DIFF_0_1);   // differential A0-A1
    adc.StartSingleConv();
    while (adc.IsBusy()) { /* spin */ }
    return adc.GetResultVolt();          // millivolts → already in volts via library
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

void setup()
{
    Serial.begin(115200);
    Wire.begin();

    Serial.println("ID:USPHERE_LOCKIN_READER");

    if (adc.Init()) {
        Serial.println("# ADC OK");
    } else {
        Serial.println("# ADC FAIL");
    }

    // ±4.096V full-scale range — accommodates ±2V from divider with headroom
    adc.SetFullScaleRange(ADS1115_PGA_4096);

    // 860 SPS for maximum speed
    adc.SetDataRate(ADS1115_SPS_860);
}

// ---------------------------------------------------------------------------
// Main loop
// ---------------------------------------------------------------------------

void loop()
{
    static unsigned long lastOutputUs = 0;
    static String        serialBuf    = "";

    // --- Handle incoming serial commands ---
    while (Serial.available()) {
        char c = Serial.read();
        if (c == '\n' || c == '\r') {
            serialBuf.trim();
            if (serialBuf.length() > 0) {
                handleCommand(serialBuf);
            }
            serialBuf = "";
        } else {
            serialBuf += c;
        }
    }

    // --- Timed output ---
    unsigned long nowUs = micros();
    if (nowUs - lastOutputUs < periodUs) {
        return;  // not yet time for next output
    }
    lastOutputUs = nowUs;

    // Average multiple readings for noise reduction
    float sum = 0.0f;
    for (unsigned int i = 0; i < avgCount; i++) {
        sum += readDifferential();
    }
    float avgVolt = sum / avgCount;

    // Reconstruct actual voltage (undo divider)
    float actualVolt = avgVolt * DIVIDER_RATIO;

    // Output: "V:+x.xxxxxx"
    Serial.print("V:");
    if (actualVolt >= 0) Serial.print('+');
    Serial.println(actualVolt, 6);
}
