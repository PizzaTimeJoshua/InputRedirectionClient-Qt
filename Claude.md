# InputRedirectionClient-Qt

A Qt-based gamepad input redirection client for the Nintendo 3DS. This application reads input from physical game controllers connected to a computer and transmits the controller state via UDP to a 3DS console running compatible server software.

## Project Structure

```
InputRedirectionClient-Qt/
├── main.cpp                          # Single source file containing all implementation (~792 lines)
├── InputRedirectionClient-Qt.pro     # Qt project configuration file
├── README.md                         # Basic project documentation
└── LICENSE                           # Project license
```

This is a single-file project with minimal dependencies:
- **Qt5 Core** - Application framework
- **Qt5 GUI/Widgets** - User interface
- **Qt5 Network** - UDP socket communication
- **Qt5 Gamepad** - Controller input handling

## Architecture Overview

The application follows an event-driven architecture with these main components:

| Component | Location | Purpose |
|-----------|----------|---------|
| `GamepadMonitor` | `main.cpp:204-294` | Listens to controller events and updates global state |
| `TouchScreen` | `main.cpp:296-356` | Emulates 3DS touch screen (320x240 window) |
| `FrameTimer` | `main.cpp:358-367` | Sends periodic frame updates every 50ms |
| `RemapConfig` | `main.cpp:369-596` | Button remapping configuration dialog |
| `Widget` | `main.cpp:598-778` | Main application window and UI controller |
| `sendFrame()` | `main.cpp:104-202` | Core protocol - assembles and transmits data packets |

## Data Flow Pipeline

```
Physical Controller
       ↓
QGamepadManager Events (button press/release, axis movement)
       ↓
GamepadMonitor Signal Handlers
       ↓
Global State Update (buttons, lx, ly, rx, ry)
       ↓
sendFrame() Function
       ↓
Data Processing & Encoding
       ↓
UDP Packet (20 bytes) → 3DS:4950
```

## Network Protocol

### Transport Layer
- **Protocol**: UDP (connectionless)
- **Port**: 4950
- **Encoding**: Little-endian

### Packet Format (20 bytes)

| Offset | Size | Field | Description |
|--------|------|-------|-------------|
| 0-3 | 4 bytes | `hidPad` | Button states (12 bits used, active-low) |
| 4-7 | 4 bytes | `touchScreenState` | Touch X/Y coordinates + pressed flag |
| 8-11 | 4 bytes | `circlePadState` | Circle Pad (left stick) X/Y |
| 12-15 | 4 bytes | `cppState` | C-Stick X/Y + IR buttons (ZL/ZR) |
| 16-19 | 4 bytes | `interfaceButtons` | Home, Power, Power-Long buttons |

## Button Input Transmission

### Button Mapping Arrays

Buttons are organized into four groups (`main.cpp:72-96`):

```cpp
hidButtonsAB[]     // A, B buttons → bits 0-1
hidButtonsMiddle[] // Select, Start, D-pad, L, R → bits 2-9
hidButtonsXY[]     // X, Y buttons → bits 10-11
irButtons[]        // ZL, ZR triggers → separate IR state
```

### Button State Encoding

The `hidPad` field uses **active-low logic** (bit = 0 means pressed):

```cpp
// From sendFrame() at main.cpp:106-145
u32 hidPad = 0xfff;  // All buttons released (12 bits set)

// For each pressed button, clear its corresponding bit:
if(buttons & (1 << hidButtonsAB[i]))
    hidPad &= ~(1 << i);  // Clear bit = button pressed
```

### Button Bit Layout

| Bit | Button |
|-----|--------|
| 0 | A |
| 1 | B |
| 2 | Select |
| 3 | Start |
| 4 | D-Pad Right |
| 5 | D-Pad Left |
| 6 | D-Pad Up |
| 7 | D-Pad Down |
| 8 | R |
| 9 | L |
| 10 | X |
| 11 | Y |

### A/B and X/Y Inversion

The application supports swapping A↔B and X↔Y buttons (`main.cpp:107-145`):

```cpp
if(!abInverse) {
    // Normal: hidButtonsAB[0] → bit 0 (A), hidButtonsAB[1] → bit 1 (B)
} else {
    // Inverted: hidButtonsAB[1] → bit 0 (A), hidButtonsAB[0] → bit 1 (B)
}
```

## Joystick (Circle Pad) Transmission

The left analog stick maps to the 3DS Circle Pad (`main.cpp:167-175`):

### Processing

```cpp
#define CPAD_BOUND 0x5d0  // 1488 in decimal

// Raw input: -1.0 to +1.0 (from Qt)
// Output: 0x000 to 0xfff (12-bit per axis)
// Center: 0x800

u32 x = (u32)(lx * CPAD_BOUND + 0x800);
u32 y = (u32)(ly * CPAD_BOUND + 0x800);

// Clamp to valid range
x = x >= 0xfff ? (lx < 0.0 ? 0x000 : 0xfff) : x;
y = y >= 0xfff ? (ly < 0.0 ? 0x000 : 0xfff) : y;

// Pack into 24-bit value: Y in upper 12 bits, X in lower 12 bits
circlePadState = (y << 12) | x;
```

### Data Format

```
circlePadState (32 bits):
┌────────────────────────────────┐
│ unused(8) │ Y coord(12) │ X coord(12) │
└────────────────────────────────┘
Default (center): 0x7ff7ff
```

### Y-Axis Inversion

Qt inverts the Y-axis from what the 3DS expects. The application compensates (`main.cpp:280`):

```cpp
ly = yAxisMultiplier * -value;  // -value to correct Qt's inversion
                                // yAxisMultiplier allows user to invert again
```

## C-Stick Transmission

The right analog stick maps to the 3DS C-Stick (Circle Pad Pro) with a **special 45-degree rotation** (`main.cpp:177-186`):

### Why Rotation is Needed

Nintendo's 3DS hardware expects C-Stick coordinates rotated 45 degrees from standard axes. This is a hardware quirk that must be compensated for in software.

### Processing

```cpp
#define CPP_BOUND 0x7f  // 127 in decimal

// Apply 45° rotation using rotation matrix:
// x' = (x + y) / √2
// y' = (y - x) / √2

u32 x = (u32)(M_SQRT1_2 * (rx + ry) * CPP_BOUND + 0x80);
u32 y = (u32)(M_SQRT1_2 * (ry - rx) * CPP_BOUND + 0x80);

// Raw input: -1.0 to +1.0
// Output: 0x00 to 0xff (8-bit per axis)
// Center: 0x80

// Clamp to valid range
x = x >= 0xff ? (rx < 0.0 ? 0x00 : 0xff) : x;
y = y >= 0xff ? (ry < 0.0 ? 0x00 : 0xff) : y;
```

### Data Format

```
cppState (32 bits):
┌─────────────────────────────────────────┐
│ Y coord(8) │ X coord(8) │ IR btns(8) │ 0x81(8) │
└─────────────────────────────────────────┘
Default (center, no buttons): 0x80800081
```

### IR Button State (ZL/ZR)

The ZL and ZR triggers are encoded in the IR buttons byte (`main.cpp:147-152`):

```cpp
u32 irButtonsState = 0;
for(u32 i = 0; i < 2; i++) {
    if(buttons & (1 << irButtons[i]))
        irButtonsState |= 1 << (i + 1);  // ZR → bit 1, ZL → bit 2
}

cppState = (y << 24) | (x << 16) | (irButtonsState << 8) | 0x81;
```

## Touch Screen Transmission

Touch input can come from mouse clicks on the TouchScreen window or from mapped controller buttons (`main.cpp:188-193`):

### Data Format

```
touchScreenState (32 bits):
┌────────────────────────────────────────────┐
│ unused(7) │ pressed(1) │ Y coord(12) │ X coord(12) │
└────────────────────────────────────────────┘
Default (not pressed): 0x2000000
```

### Processing

```cpp
// Screen dimensions: 320x240 (matching 3DS touch screen)
// Output: 0x000 to 0xfff (12-bit per axis)

u32 x = (u32)(0xfff * touchScreenPosition.x()) / TOUCH_SCREEN_WIDTH;
u32 y = (u32)(0xfff * touchScreenPosition.y()) / TOUCH_SCREEN_HEIGHT;
touchScreenState = (1 << 24) | (y << 12) | x;  // Set pressed bit
```

## Interface Buttons

Home, Power, and Power-Long buttons use separate state (`main.cpp:16-19`):

| Bit | Button |
|-----|--------|
| 0 | Home |
| 1 | Power |
| 2 | Power (long press) |

## Global State Variables

| Variable | Type | Purpose |
|----------|------|---------|
| `lx`, `ly` | `double` | Left stick X/Y (-1.0 to 1.0) |
| `rx`, `ry` | `double` | Right stick X/Y (-1.0 to 1.0) |
| `buttons` | `u32` | Button bitmask from QGamepadManager |
| `interfaceButtons` | `u32` | Home/Power button state |
| `touchScreenPressed` | `bool` | Touch state |
| `touchScreenPosition` | `QPoint` | Touch coordinates (0-319, 0-239) |
| `yAxisMultiplier` | `int` | Y-axis inversion (+1 or -1) |
| `abInverse`, `xyInverse` | `bool` | Button swap flags |

## Configuration Persistence

Settings are stored via `QSettings` with organization "TuxSH" and application "InputRedirectionClient-Qt" (`main.cpp:50`):

- `ipAddress` - Target 3DS IP address
- `invertY`, `invertAB`, `invertXY` - Inversion toggles
- `ButtonA` through `ButtonZR` - Button remappings
- `ButtonHome`, `ButtonPower`, `ButtonPowerLong` - Interface button mappings
- `ButtonT1`, `ButtonT2` - Touch shortcut button mappings
- `touchButton1X/Y`, `touchButton2X/Y` - Touch shortcut coordinates

## Multi-Controller Support

Multiple connected controllers are automatically merged. The `GamepadMonitor` listens to events from all controllers via `QGamepadManager`, and button/axis states are combined into the global state variables.

## Build Instructions

Requires Qt5 with the following modules:
- `core`, `gui`, `widgets`, `network`, `gamepad`

```bash
qmake InputRedirectionClient-Qt.pro
make
```

## Platform Support

- **Windows**: XInput (Xbox controllers, or use x360ce for other controllers)
- **Linux**: evdev
- **macOS**: Native gamepad support
