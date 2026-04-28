# SR530 User Guide

A practical guide to using the SR530 lock-in amplifier with this GUI, written for someone who understands the mathematics of lock-in detection but is operating the physical instrument for the first time.

---

## Contents

1. [From software to hardware — what's different](#1-from-software-to-hardware--whats-different)
2. [Physical connections](#2-physical-connections)
3. [Startup sequence](#3-startup-sequence)
4. [Connection tab](#4-connection-tab)
5. [Parameters tab — a guide to every setting](#5-parameters-tab--a-guide-to-every-setting)
6. [Monitor tab — reading the outputs](#6-monitor-tab--reading-the-outputs)
7. [Setting the phase](#7-setting-the-phase)
8. [Choosing sensitivity](#8-choosing-sensitivity)
9. [Choosing time constants](#9-choosing-time-constants)
10. [Dynamic reserve](#10-dynamic-reserve)
11. [Overload and unlock indicators](#11-overload-and-unlock-indicators)
12. [Calibration workflow (usphere-Q)](#12-calibration-workflow-usphere-q)
13. [Typical session walkthrough](#13-typical-session-walkthrough)
14. [Common mistakes](#14-common-mistakes)

---

## 1. From software to hardware — what's different

When you implement lock-in detection in software (multiplying a signal by a reference tone, then low-pass filtering), you have exact control over every parameter and can re-run the analysis on recorded data. The SR530 does the same thing in analog hardware, but several things work differently:

**The reference is live, not recorded.**
The SR530 needs a continuous reference signal at the drive frequency right now. It either generates its own reference internally (oscillator mode) or locks to an external TTL/sine signal you provide on the rear REF IN connector. For the microsphere experiment the drive waveform generator is your reference source.

**The output is a continuous voltage, not a number you compute.**
The front-panel outputs (X, Y) are real analog voltages between −10 V and +10 V, representing the in-phase and quadrature components of your signal scaled to the current sensitivity setting. The GUI reads these by querying the instrument over RS-232; the values you see in the Monitor tab are those voltages digitized by the SR530's internal ADC.

**Sensitivity is a hardware gain stage, not just a display scale.**
Setting a low sensitivity (e.g. 100 nV full-scale) applies very high gain inside the amplifier. If your signal is larger than the sensitivity range the input stage clips — this is an overload — and the reading is meaningless. You must set sensitivity large enough that your signal fits, and small enough that noise doesn't swamp the resolution.

**Time constants are analog RC filters, not an FFT window.**
In software you control the effective bandwidth by choosing how many cycles to average. On the SR530, the post-filter time constant τ is an RC low-pass filter after the phase-sensitive detector. A longer τ = narrower bandwidth = slower response but better noise rejection. The relationship is: bandwidth ≈ 1/(4τ) for a single-pole filter.

**Phase must be set on the instrument, not corrected in post.**
In offline analysis you can freely rotate the complex vector (X + iY) after the fact. On the SR530 the phase offset is set before the detector. If the phase is wrong, signal leaks from X into Y and your X reading underestimates the true signal amplitude.

---

## 2. Physical connections

```
Drive waveform generator (RIGOL DG822 / INSTEK AFG-2225)
    CH1 (sine, drive frequency) ──────────────► SR530 REFERENCE IN (rear)
                                                  (use 50 Ω BNC cable)

Microsphere detector signal (photodetector output)
    ────────────────────────────────────────────► SR530 SIGNAL IN (front, A input)
                                                  (use shielded BNC, keep short)

SR530 X OUTPUT (front, BNC)  ──────────────────► ESP32 ADS1115 analog input
                                   OR
SR530 RS-232 (rear, DB-25)   ──────────────────► USB-RS232 adapter ──► PC
```

**Input mode:** Use the A input (single-ended) unless you have a differential signal. Set the front-panel INPUT switch to A.

**Reference mode:** Use EXT (external reference). The SR530 will lock to the frequency of the signal on REF IN. The front-panel REFERENCE MODE switch should be set to EXT. If it cannot lock (the UNLOCK LED stays lit) check that the reference signal is above the minimum amplitude (~100 mV pk-pk) and that the frequency is stable.

**Grounding:** Make sure the signal source and SR530 share a common ground. Use the GND post on the front panel if needed.

---

## 3. Startup sequence

1. Power on the SR530 before connecting RS-232 — the instrument boots and initialises its serial interface.
2. Apply the reference signal first, then the input signal. This lets the SR530 lock to the reference before it sees the signal.
3. Confirm the UNLOCK LED is off (reference is locked) before trusting any readings.
4. Start with a high sensitivity setting (e.g. 500 mV) so you cannot overload, then reduce once you see a stable reading.

---

## 4. Connection tab

Open the GUI and go to the **Connection** tab.

| Field | What to enter |
|-------|---------------|
| **Port** | The COM port assigned to your USB-RS232 adapter. Check Device Manager (Windows) or `ls /dev/ttyUSB*` (Linux). Typical values: COM3–COM10 on Windows. |
| **Baud rate** | Must match the DIP switch setting on the rear of the SR530. Default from the factory is 9600. |
| **Timeout** | Leave at 2.0 s. Increase only if you see timeout errors on a slow/virtual COM port. |

Click **Connect**. The status turns green and the Parameters and Monitor tabs unlock.

Click **Test (read frequency)** — this sends a single query (`F`) to the SR530 and prints the reference frequency in the log. If you get a sensible number (e.g. 100.0 Hz) the connection is working. If you get a timeout or garbled response:
- Check the COM port number
- Check the baud rate matches the DIP switches (see SR530 manual §6 — the rear label also shows the current setting)
- Check the USB-RS232 adapter driver is installed

---

## 5. Parameters tab — a guide to every setting

After connecting, the Parameters tab is enabled. Click **Read from instrument** first — this loads the current front-panel settings into the GUI so you can see what the SR530 is actually set to before you change anything.

### Phase

The reference phase offset in degrees (−180° to +180°).

This shifts the SR530's internal reference oscillator relative to the external REF IN signal. When it is set correctly, all of your signal appears in the X (in-phase) output and Y is near zero. See [Section 7](#7-setting-the-phase) for the procedure.

### Sensitivity

The full-scale range of the X and Y output voltages (±10 V out = ±1 × full-scale internally).

| Index | Full-scale | Use when signal amplitude is... |
|-------|------------|---------------------------------|
| 4     | 100 nV     | < 100 nV — very low noise environment only |
| 10    | 10 µV      | < 10 µV |
| 16    | 1 mV       | < 1 mV |
| 19    | 10 mV      | < 10 mV |
| 22    | 100 mV     | < 100 mV |
| 24    | 500 mV     | < 500 mV (maximum range) |

Indices 1–3 require the optional SRS pre-amplifier and should not be selected without it. Start high (index 22–24) and reduce until the signal occupies 10–80% of full-scale without triggering the OVERLOAD indicator. See [Section 8](#8-choosing-sensitivity).

### Dynamic reserve

How much headroom the amplifier has for signals at other frequencies (noise, interference).

| Setting | When to use |
|---------|-------------|
| **Low Noise** | Your signal dominates; there are no large off-frequency interferers. Best noise performance. Use this for the microsphere experiment when the trap is stable. |
| **Normal** | General purpose. Moderate noise, moderate interference handling. |
| **High Reserve** | Large interfering signals at other frequencies (e.g. 60 Hz pickup much larger than your drive signal). Worse noise floor, but the amplifier will not clip on the interference. |

See [Section 10](#10-dynamic-reserve) for more detail.

### Pre time constant (T1)

A bandpass/high-pass filter applied to the signal **before** the phase-sensitive detector.

For most uses, set this to a value much shorter than your drive period (1/f₀). For a 100 Hz drive:
- Drive period = 10 ms
- Pre TC: 1 ms (index 1) or 3 ms (index 2) is appropriate

A longer pre TC can help reject DC and very low-frequency drift before detection, but if it is too long relative to the drive period it will distort the signal being detected.

### Post time constant (T2)

The main integration filter applied **after** the phase-sensitive detector. This is the parameter that determines how long the instrument averages.

This is the hardware equivalent of your software averaging window. The SR530 has **only three options** for the post-filter time constant:

| Setting | Value | Noise bandwidth | Response to step change |
|---------|-------|-----------------|------------------------|
| Off (0) | None  | Set by pre-TC only | Immediate |
| 100 ms (1) | 100 ms | ~2.5 Hz | Settles in ~300 ms |
| 1 s (2)    | 1 s   | ~0.25 Hz | Settles in ~3 s |

The pre-filter time constant (T1) provides the majority of the integration and has 11 options from 1 ms to 100 s. In most cases, T1 is your primary noise-rejection knob.

For the microsphere experiment, the choice depends on how fast your charge state changes:
- During a charge jump event the reading must settle before the next measurement — use T1 ≤ your measurement interval / 5
- During steady-state monitoring, T1 = 1 s (index 7) or 3 s (index 8) gives a clean reading

**Rule of thumb:** T1 should be at least 10× the drive period (10 cycles), and short enough that the reading settles between charge events.

### Apply all / Read from instrument

- **Apply all** sends all currently displayed values to the SR530. Changes take effect immediately.
- **Read from instrument** queries the SR530 and updates the GUI to match the actual current settings. Always do this after connecting.

---

## 6. Monitor tab — reading the outputs

Enable **Auto-refresh** and set the interval. 500 ms is a good starting point; reduce to 200 ms if you need faster updates (note each snapshot takes ~8 serial round-trips at 9600 baud, so below ~100 ms you will start missing cycles).

### Output columns

| Output | What it is | Units shown |
|--------|-----------|-------------|
| **X (in-phase)** | Component of your signal in phase with the reference | Fraction of FS  ·  real volts |
| **Y (quadrature)** | Component 90° out of phase with the reference | Fraction of FS  ·  real volts |
| **R (magnitude)** | √(X² + Y²) — signal amplitude regardless of phase | Fraction of FS  ·  real volts |
| **θ (phase)** | arctan(Y/X) — measured phase of your signal | Degrees |
| **Frequency** | Reference frequency (what REF IN is locked to) | Hz |
| **Sensitivity** | Current full-scale range | Label string |

**In-phase (X)** is the quantity you care about for the microsphere charge measurement, because the drive force and the sphere response are in a known phase relationship. Once you have set the phase correctly (Section 7), X directly gives you the amplitude of the sphere's response to the drive.

**R** is useful for initially finding your signal without worrying about phase, and for checking that the total signal amplitude is stable.

**θ** is useful for diagnosing phase drift — if θ wanders, the sphere's resonant frequency may have shifted, or the reference is unstable.

### Status indicators

**Overload: YES** (red) — the input signal is too large for the current sensitivity setting. The X and Y readings are clipped and meaningless. Increase sensitivity (higher index = larger range) immediately.

**Reference: UNLOCKED** (amber) — the SR530 cannot phase-lock to the REF IN signal. Possible causes:
- Reference cable disconnected or too weak (< ~100 mV pk-pk)
- Reference frequency changed abruptly
- Reference noise is too high

All readings during an unlock event are invalid. The instrument will relock automatically when the reference is restored.

---

## 7. Setting the phase

The phase setting is the most important parameter to get right. An incorrect phase causes your signal to appear split between X and Y, making X smaller than it should be (by a factor of cos(Δφ)) and making Y non-zero.

**Procedure:**

1. With the sphere trapped and the drive applied, open the Monitor tab and start auto-refresh.
2. Observe X and Y while the system is at a known charge state (ideally a single charge).
3. Adjust the phase (Parameters tab) until Y is minimised (as close to zero as possible) and X is maximised.
4. Note the sign of X — if it is negative, add or subtract 180° to flip it positive (or leave it negative and account for the sign in calibration).
5. Click **Apply all** after each phase change and wait one or two post-TC settling times before judging the result.

**Mathematically:** if the true signal amplitude is A and the phase error is Δφ, then X = A·cos(Δφ) and Y = A·sin(Δφ). When Δφ = 0, X = A and Y = 0. You are minimising Y, not maximising R — R is phase-independent and doesn't tell you whether the phase is right.

**Tip:** do not change the phase while a charge measurement is in progress. Phase changes cause a transient in the output that takes several post-TCs to settle.

---

## 8. Choosing sensitivity

**Start high, work down.**

1. Set sensitivity to index 21 (1 V full-scale). You cannot overload at this setting unless you have a very large signal.
2. Watch R in the Monitor tab. Wait for the reading to stabilise (allow ~5× your post TC).
3. If R is less than 10% of full-scale, decrease sensitivity by 3–6 steps (halve or third the range) and wait again.
4. Stop when R is between 10% and 80% of full-scale without triggering OVERLOAD.

**Why 10–80%?**
- Below 10%: you are using only a small fraction of the ADC range — unnecessary digitisation noise.
- Above 80%: a small increase in signal (e.g. a charge jump) will overload.
- Above 100%: OVERLOAD indicator fires, reading is clipped.

**After a charge jump:** if the sphere picks up extra charge the signal amplitude rises. If it overloads at your current sensitivity, increase the range (higher index) before the next measurement.

---

## 9. Choosing time constants

### Pre time constant (T1)

Set to ≤ 1/f₀. For a 100 Hz drive: use 1 ms (index 1) or 3 ms (index 2).

You rarely need to change this during normal operation. Leave it at 1 ms unless you have a specific reason (e.g. to reject a large DC offset before detection).

### Post time constant (T2)

This is your main tuning knob for noise vs. speed.

**For continuous monitoring of a stable sphere:** use T1 = 300 ms (index 6) or 1 s (index 7), with T2 = Off or 100 ms. This gives ~1–3 Hz bandwidth, filtering most mechanical and electronic noise, while still responding to charge events within a few seconds.

**For fast charge-change detection:** use T1 = 30–100 ms (indices 4–5), T2 = Off. You trade noise rejection for faster response.

**For calibration (averaging a known charge state):** use T1 = 1–10 s (indices 7–9), T2 = 1 s. Take a reading only after waiting ≥ 5τ from the last disturbance.

**Never set T2 shorter than 3–5 drive periods.** The phase-sensitive detector needs at least a few reference cycles to form a meaningful average. At 100 Hz drive, 5 cycles = 50 ms, so 30 ms TC is the practical minimum.

---

## 10. Dynamic reserve

Think of dynamic reserve as the amplifier's ability to handle signals it does not care about.

Your signal is at frequency f₀. Everything else — 60 Hz mains pickup, acoustic noise, vibration — is also present at the input but at different frequencies. The SR530 rejects these by phase-sensitive detection, but if they are much larger than your signal they can drive the pre-amplifier into clipping before detection happens.

**Low Noise:** the pre-amplifier has maximum gain, minimum noise, but cannot handle large off-frequency signals. Use this when your lab environment is quiet and shielding is good.

**Normal:** moderate gain, moderate noise. The safe default for most situations.

**High Reserve:** lower pre-amplifier gain, higher noise floor, but can handle interfering signals 60 dB larger than your signal. Use this if you see the OVERLOAD indicator firing intermittently even at a sensitivity setting that should be adequate — this usually means an interferer is clipping the pre-amp.

**Practical rule:** use Low Noise unless OVERLOAD fires unexpectedly. If it does, switch to Normal. If it still fires, switch to High Reserve and investigate the source of interference.

---

## 11. Overload and unlock indicators

### OVERLOAD (input overload)

The instrument's input amplifier or detector is saturated.

**Immediate action:** increase sensitivity (higher index) until OVERLOAD clears. Do not trust any reading taken while OVERLOAD was lit — the output was clipped.

**If OVERLOAD persists after increasing sensitivity to maximum (index 24, 500 mV):** your signal is too large for the SR530 or you have a wiring fault. Check the input connection.

**If OVERLOAD fires intermittently:** switch Dynamic Reserve to Normal or High Reserve. Large off-frequency noise is clipping the pre-amplifier. Improve shielding or reduce interference if possible, then return to Low Noise.

### Reference UNLOCKED

The SR530 has lost lock with the external reference.

**Immediate action:** do not record any data. Check:
1. The REF IN cable is connected and the signal is present (use an oscilloscope or the monitor on the waveform generator).
2. The reference amplitude is adequate (> ~100 mV pk-pk at REF IN).
3. The drive frequency has not changed abruptly. If it has, the SR530 will relock within a few seconds automatically.

The SR530 will display the last locked frequency while unlocked. Once the reference is restored, wait one full post-TC settling time before trusting the output again.

---

## 12. Calibration workflow (usphere-Q)

The lock-in X output gives you a voltage proportional to the sphere's driven displacement amplitude, which is proportional to the charge. To convert X output voltage → number of electrons, you need a **volts-per-electron** calibration factor.

### Procedure

1. Prepare the sphere in a known charge state (ideally ±1e, verified independently or by comparison with the file-based checkQ analysis).
2. In the Monitor tab, set a long post TC (1–3 s) and wait for the reading to stabilise (wait ≥ 5τ).
3. Record the X output voltage displayed (e.g. `x_v = +0.00312 V`).
4. Go to the **Calibration** tab in usphere-Q, enter the measured voltage and the known charge, and click **Calibrate lock-in**.
5. The computed `volts_per_electron = voltage / charge` is saved to the calibration JSON file.
6. Enter this value in the Analysis tab → Lock-in (SR530 direct) → **Volts per electron** field.

### Notes

- Calibrate at the sensitivity setting you plan to use for measurements. Changing sensitivity changes the gain, which changes the voltage for the same charge. If you change sensitivity you must recalibrate or scale accordingly (new_vpe = old_vpe × new_sens_v / old_sens_v).
- The phase must be correctly set before calibrating, or the X output will underestimate the true amplitude.
- If the sphere has both positive and negative charge states during your session, check that the sign of X is consistent with polarity — positive charge should give a definite sign of X (positive or negative depending on your phase setting). Record this convention.

---

## 13. Typical session walkthrough

```
1. Power on SR530 and waveform generator
2. Apply drive signal to sphere AND to SR530 REF IN
3. Launch GUI:  python sr530_gui.py
4. Connection tab → enter COM port → Connect → Test (read frequency)
   ✓ frequency should match your drive frequency
5. Parameters tab → Read from instrument
6. Set sensitivity to index 22 (100 mV) as a starting point
7. Set dynamic reserve to Normal
8. Set pre TC to index 5 (100 ms), post TC to Off initially
9. Apply all
10. Monitor tab → enable Auto-refresh (500 ms)
    ✓ Reference: Locked (green)
    ✓ Overload: OK (green)
11. Observe R — adjust sensitivity until R is 10–80% of FS (no OVERLOAD)
12. Set phase: adjust until Y ≈ 0, X is maximised → Apply all → wait 2–3 TC
13. Increase post TC to your working value (300 ms – 1 s) → Apply all
14. Switch to Low Noise if environment is clean
15. Read calibration voltage at known charge state → enter in Calibration tab
16. Enable Analysis tab in usphere-Q → source = Lock-in (SR530 direct) → Start monitoring
```

---

## 14. Common mistakes

**Trusting a reading immediately after changing a parameter.**
The output takes ~5× the post TC to settle after any change. After setting phase, sensitivity, or TC, wait before reading.

**Leaving phase at 0° and wondering why X is small.**
The default phase is 0°, which is rarely correct. An unset phase is the most common reason for unexpectedly small X readings. Always set the phase (Section 7) before calibrating or measuring.

**Setting the post TC too short.**
A TC shorter than 5 drive periods causes the phase-sensitive detector to average over fewer than 5 cycles, which means significant harmonic content at 2f₀ leaks through the filter. The output oscillates at 2f₀ rather than settling to a steady value.

**Comparing lock-in readings taken at different sensitivity settings.**
A change in sensitivity changes the hardware gain. A reading of +0.003 V at 1 mV sensitivity is NOT the same charge as +0.003 V at 10 mV sensitivity. Always note sensitivity alongside voltage readings, and recalibrate if you change it.

**Ignoring OVERLOAD and recording data anyway.**
A clipped reading looks like a legitimate (but smaller) signal. It will not crash the software; it will silently give you wrong data. Treat any reading taken during OVERLOAD as invalid.

**Expecting the same noise floor as offline analysis.**
The SR530 uses analog filters, not an ideal digital integrator. It adds its own noise (see the noise spec in the SR530 manual, typically ~6 nV/√Hz input noise). In practice at 9600 baud the minimum useful poll interval is ~100 ms (limited by serial speed), so you cannot average as finely as a long offline FFT.
