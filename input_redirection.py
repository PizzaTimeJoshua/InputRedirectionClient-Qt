#!/usr/bin/env python3
"""
Nintendo 3DS Input Redirection Client

This module implements the UDP protocol used to send controller input
to a Nintendo 3DS running input redirection server software (like InputRedirection
NTR plugin or Luma3DS input redirection).

Protocol Details:
- Transport: UDP
- Port: 4950
- Packet Size: 20 bytes (little-endian)
- Frame Rate: Recommended 50ms intervals (20 Hz)

Packet Structure (20 bytes):
    Offset 0-3:   hidPad (button states, active-low)
    Offset 4-7:   touchScreenState (touch position + pressed flag)
    Offset 8-11:  circlePadState (left analog stick)
    Offset 12-15: cppState (C-Stick + ZL/ZR buttons)
    Offset 16-19: interfaceButtons (Home, Power, Power-Long)

Usage:
    client = InputRedirectionClient("192.168.1.100")
    client.press_button(Button.A)
    client.set_circle_pad(0.5, -0.3)
    client.send_frame()
"""

import socket
import struct
import math
from enum import IntEnum, IntFlag
from dataclasses import dataclass
from typing import Optional, Tuple


# =============================================================================
# CONSTANTS
# =============================================================================

# UDP port the 3DS listens on for input redirection
INPUT_REDIRECTION_PORT = 4950

# Circle Pad (left stick) boundaries
# The Circle Pad uses 12-bit coordinates (0x000 to 0xFFF)
# Center position is 0x800, and CPAD_BOUND defines the range from center
CPAD_BOUND = 0x5D0  # 1488 in decimal - maximum deflection from center

# C-Stick (right stick) boundaries
# The C-Stick uses 8-bit coordinates (0x00 to 0xFF)
# Center position is 0x80
CPP_BOUND = 0x7F  # 127 in decimal - maximum deflection from center

# Touch screen dimensions (matches 3DS bottom screen)
TOUCH_SCREEN_WIDTH = 320
TOUCH_SCREEN_HEIGHT = 240

# Square root of 1/2, used for 45-degree rotation of C-Stick
# The 3DS hardware expects C-Stick values rotated 45 degrees
SQRT_1_2 = math.sqrt(0.5)  # ~0.7071067811865476


# =============================================================================
# BUTTON DEFINITIONS
# =============================================================================

class Button(IntFlag):
    """
    3DS button bit flags for the hidPad field.

    IMPORTANT: The hidPad field uses ACTIVE-LOW logic!
    - Bit = 0 means button is PRESSED
    - Bit = 1 means button is RELEASED

    The default state (all buttons released) is 0xFFF (all 12 bits set).
    To press a button, you CLEAR its corresponding bit.

    Bit layout in hidPad:
        Bit 0:  A button
        Bit 1:  B button
        Bit 2:  Select
        Bit 3:  Start
        Bit 4:  D-Pad Right
        Bit 5:  D-Pad Left
        Bit 6:  D-Pad Up
        Bit 7:  D-Pad Down
        Bit 8:  R shoulder
        Bit 9:  L shoulder
        Bit 10: X button
        Bit 11: Y button
    """
    NONE = 0
    A = 1 << 0       # Bit 0
    B = 1 << 1       # Bit 1
    SELECT = 1 << 2  # Bit 2
    START = 1 << 3   # Bit 3
    DPAD_RIGHT = 1 << 4  # Bit 4
    DPAD_LEFT = 1 << 5   # Bit 5
    DPAD_UP = 1 << 6     # Bit 6
    DPAD_DOWN = 1 << 7   # Bit 7
    R = 1 << 8       # Bit 8
    L = 1 << 9       # Bit 9
    X = 1 << 10      # Bit 10
    Y = 1 << 11      # Bit 11


class IRButton(IntFlag):
    """
    IR buttons (ZL/ZR) for the Circle Pad Pro / New 3DS.

    These are transmitted in the cppState field, NOT in hidPad.
    They occupy bits 1-2 of the IR button byte (byte offset 14 in packet).

    Unlike hidPad, these use ACTIVE-HIGH logic:
    - Bit = 1 means button is PRESSED
    - Bit = 0 means button is RELEASED
    """
    NONE = 0
    ZR = 1 << 1  # Bit 1 of IR button state
    ZL = 1 << 2  # Bit 2 of IR button state


class InterfaceButton(IntFlag):
    """
    Special interface buttons (Home, Power).

    These are transmitted in the interfaceButtons field (bytes 16-19).
    Uses ACTIVE-HIGH logic.
    """
    NONE = 0
    HOME = 1 << 0        # Bit 0 - Opens HOME menu
    POWER = 1 << 1       # Bit 1 - Short power press
    POWER_LONG = 1 << 2  # Bit 2 - Long power press (power off dialog)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class TouchState:
    """
    Represents the current touch screen state.

    Attributes:
        pressed: Whether the touch screen is being touched
        x: X coordinate (0 to 319, left to right)
        y: Y coordinate (0 to 239, top to bottom)
    """
    pressed: bool = False
    x: int = 0
    y: int = 0


@dataclass
class StickState:
    """
    Represents an analog stick's current position.

    Attributes:
        x: Horizontal position (-1.0 = full left, 0.0 = center, 1.0 = full right)
        y: Vertical position (-1.0 = full down, 0.0 = center, 1.0 = full up)

    Note: The 3DS uses Y-up convention (positive Y = up), which matches
    standard gamepad conventions.
    """
    x: float = 0.0
    y: float = 0.0


# =============================================================================
# MAIN CLIENT CLASS
# =============================================================================

class InputRedirectionClient:
    """
    Client for sending input to a Nintendo 3DS via UDP.

    This class manages the complete input state and handles encoding
    the data into the correct packet format expected by the 3DS.

    Example usage:
        # Create client and connect to 3DS
        client = InputRedirectionClient("192.168.1.100")

        # Press and hold the A button
        client.press_button(Button.A)
        client.send_frame()

        # Move the Circle Pad right
        client.set_circle_pad(1.0, 0.0)
        client.send_frame()

        # Release all inputs
        client.reset_all()
        client.send_frame()
    """

    def __init__(self, ip_address: str, port: int = INPUT_REDIRECTION_PORT):
        """
        Initialize the input redirection client.

        Args:
            ip_address: IP address of the 3DS console
            port: UDP port (default: 4950)
        """
        self.ip_address = ip_address
        self.port = port

        # Create UDP socket for sending packets
        # UDP is connectionless, so we don't need to establish a connection
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Current button state (which buttons are pressed)
        # This uses our Button flags, NOT the inverted hidPad format
        self._buttons: Button = Button.NONE

        # IR buttons (ZL/ZR) - stored separately as they go in cppState
        self._ir_buttons: IRButton = IRButton.NONE

        # Interface buttons (Home, Power)
        self._interface_buttons: InterfaceButton = InterfaceButton.NONE

        # Analog stick states
        self._circle_pad = StickState()  # Left stick
        self._c_stick = StickState()     # Right stick (Circle Pad Pro / New 3DS)

        # Touch screen state
        self._touch = TouchState()

    # =========================================================================
    # BUTTON METHODS
    # =========================================================================

    def press_button(self, button: Button) -> None:
        """
        Press one or more buttons (add to currently pressed buttons).

        Args:
            button: Button(s) to press. Can combine with | operator.

        Example:
            client.press_button(Button.A)
            client.press_button(Button.A | Button.B)  # Press both A and B
        """
        self._buttons |= button

    def release_button(self, button: Button) -> None:
        """
        Release one or more buttons.

        Args:
            button: Button(s) to release. Can combine with | operator.
        """
        self._buttons &= ~button

    def set_buttons(self, buttons: Button) -> None:
        """
        Set the exact button state (replaces current state).

        Args:
            buttons: Complete button state to set.

        Example:
            client.set_buttons(Button.A | Button.START)  # Only A and START pressed
        """
        self._buttons = buttons

    def release_all_buttons(self) -> None:
        """Release all standard buttons (A, B, X, Y, D-pad, L, R, Start, Select)."""
        self._buttons = Button.NONE

    # =========================================================================
    # IR BUTTON METHODS (ZL/ZR)
    # =========================================================================

    def press_ir_button(self, button: IRButton) -> None:
        """
        Press ZL and/or ZR buttons.

        These buttons are part of the Circle Pad Pro accessory on original 3DS
        or built into the New 3DS. They're transmitted separately from main buttons.

        Args:
            button: IRButton.ZL, IRButton.ZR, or both combined with |
        """
        self._ir_buttons |= button

    def release_ir_button(self, button: IRButton) -> None:
        """Release ZL and/or ZR buttons."""
        self._ir_buttons &= ~button

    def set_ir_buttons(self, buttons: IRButton) -> None:
        """Set the exact ZL/ZR button state."""
        self._ir_buttons = buttons

    # =========================================================================
    # INTERFACE BUTTON METHODS (HOME/POWER)
    # =========================================================================

    def press_interface_button(self, button: InterfaceButton) -> None:
        """
        Press Home or Power button.

        WARNING: Power buttons can turn off the console!
        - POWER: Short press (may show power dialog)
        - POWER_LONG: Long press (forces power off)

        Args:
            button: InterfaceButton to press
        """
        self._interface_buttons |= button

    def release_interface_button(self, button: InterfaceButton) -> None:
        """Release Home or Power button."""
        self._interface_buttons &= ~button

    def press_home(self) -> None:
        """Convenience method to press the HOME button."""
        self.press_interface_button(InterfaceButton.HOME)

    def release_home(self) -> None:
        """Convenience method to release the HOME button."""
        self.release_interface_button(InterfaceButton.HOME)

    # =========================================================================
    # CIRCLE PAD (LEFT STICK) METHODS
    # =========================================================================

    def set_circle_pad(self, x: float, y: float) -> None:
        """
        Set the Circle Pad (left analog stick) position.

        The Circle Pad is the primary analog input on all 3DS models.

        Args:
            x: Horizontal position (-1.0 to 1.0, negative = left)
            y: Vertical position (-1.0 to 1.0, negative = down)

        Example:
            client.set_circle_pad(1.0, 0.0)   # Full right
            client.set_circle_pad(-1.0, 0.0)  # Full left
            client.set_circle_pad(0.0, 1.0)   # Full up
            client.set_circle_pad(0.7, 0.7)   # Diagonal up-right
        """
        # Clamp values to valid range
        self._circle_pad.x = max(-1.0, min(1.0, x))
        self._circle_pad.y = max(-1.0, min(1.0, y))

    def reset_circle_pad(self) -> None:
        """Return the Circle Pad to center position."""
        self._circle_pad.x = 0.0
        self._circle_pad.y = 0.0

    # =========================================================================
    # C-STICK (RIGHT STICK) METHODS
    # =========================================================================

    def set_c_stick(self, x: float, y: float) -> None:
        """
        Set the C-Stick (right analog stick) position.

        The C-Stick is available on:
        - New 3DS / New 3DS XL / New 2DS XL (built-in)
        - Original 3DS with Circle Pad Pro accessory

        IMPORTANT: The 3DS hardware expects C-Stick coordinates rotated 45 degrees.
        This method handles the rotation automatically - you provide standard
        X/Y coordinates and the encoding handles the rotation.

        Args:
            x: Horizontal position (-1.0 to 1.0, negative = left)
            y: Vertical position (-1.0 to 1.0, negative = down)
        """
        self._c_stick.x = max(-1.0, min(1.0, x))
        self._c_stick.y = max(-1.0, min(1.0, y))

    def reset_c_stick(self) -> None:
        """Return the C-Stick to center position."""
        self._c_stick.x = 0.0
        self._c_stick.y = 0.0

    # =========================================================================
    # TOUCH SCREEN METHODS
    # =========================================================================

    def touch(self, x: int, y: int) -> None:
        """
        Touch the screen at the specified coordinates.

        The 3DS touch screen is 320x240 pixels (bottom screen).

        Args:
            x: X coordinate (0-319, 0 = left edge)
            y: Y coordinate (0-239, 0 = top edge)

        Example:
            client.touch(160, 120)  # Touch center of screen
        """
        self._touch.pressed = True
        self._touch.x = max(0, min(TOUCH_SCREEN_WIDTH - 1, x))
        self._touch.y = max(0, min(TOUCH_SCREEN_HEIGHT - 1, y))

    def release_touch(self) -> None:
        """Release the touch screen (stop touching)."""
        self._touch.pressed = False

    # =========================================================================
    # GLOBAL STATE METHODS
    # =========================================================================

    def reset_all(self) -> None:
        """
        Reset all inputs to their default (released/centered) state.

        Call this followed by send_frame() to release all inputs.
        """
        self._buttons = Button.NONE
        self._ir_buttons = IRButton.NONE
        self._interface_buttons = InterfaceButton.NONE
        self._circle_pad = StickState()
        self._c_stick = StickState()
        self._touch = TouchState()

    # =========================================================================
    # PACKET ENCODING
    # =========================================================================

    def _encode_hid_pad(self) -> int:
        """
        Encode button states into hidPad format.

        The hidPad field uses ACTIVE-LOW encoding:
        - Start with 0xFFF (all 12 bits set = all buttons released)
        - For each pressed button, CLEAR its bit

        Returns:
            32-bit hidPad value (only lower 12 bits are used)
        """
        # Start with all buttons released (all bits set)
        hid_pad = 0xFFF

        # Clear the bit for each pressed button
        # Since our Button enum already has the correct bit positions,
        # we just need to invert the logic
        hid_pad &= ~int(self._buttons)

        return hid_pad

    def _encode_touch_screen(self) -> int:
        """
        Encode touch screen state into touchScreenState format.

        Format (32 bits):
            Bits 0-11:  X coordinate (12-bit, 0x000 to 0xFFF)
            Bits 12-23: Y coordinate (12-bit, 0x000 to 0xFFF)
            Bit 24:     Pressed flag (1 = touching, 0 = not touching)
            Bits 25-31: Unused

        The X/Y coordinates are scaled from screen pixels (320x240) to
        12-bit values (0x000 to 0xFFF).

        Returns:
            32-bit touchScreenState value
        """
        if not self._touch.pressed:
            # Default value when not touching: 0x2000000
            # This has bit 25 set but NOT bit 24 (pressed flag)
            return 0x2000000

        # Scale pixel coordinates to 12-bit range
        # Formula: (0xFFF * pixel_coord) / screen_dimension
        x = (0xFFF * self._touch.x) // TOUCH_SCREEN_WIDTH
        y = (0xFFF * self._touch.y) // TOUCH_SCREEN_HEIGHT

        # Pack: pressed flag (bit 24) | y (bits 12-23) | x (bits 0-11)
        return (1 << 24) | (y << 12) | x

    def _encode_circle_pad(self) -> int:
        """
        Encode Circle Pad state into circlePadState format.

        Format (32 bits):
            Bits 0-11:  X coordinate (12-bit)
            Bits 12-23: Y coordinate (12-bit)
            Bits 24-31: Unused

        Coordinate encoding:
            - Center position: 0x800 (2048)
            - Full left/down:  0x800 - CPAD_BOUND = 0x230 (560)
            - Full right/up:   0x800 + CPAD_BOUND = 0xDD0 (3536)
            - Valid range: 0x000 to 0xFFF

        Returns:
            32-bit circlePadState value
        """
        # Default (centered) value
        if self._circle_pad.x == 0.0 and self._circle_pad.y == 0.0:
            return 0x7FF7FF  # Slightly off-center default used by original client

        # Convert -1.0...1.0 to 12-bit coordinate centered at 0x800
        # x_out = input * CPAD_BOUND + 0x800
        x = int(self._circle_pad.x * CPAD_BOUND + 0x800)
        y = int(self._circle_pad.y * CPAD_BOUND + 0x800)

        # Clamp to valid 12-bit range
        # If we exceed 0xFFF, clamp based on direction
        if x >= 0xFFF:
            x = 0x000 if self._circle_pad.x < 0 else 0xFFF
        if y >= 0xFFF:
            y = 0x000 if self._circle_pad.y < 0 else 0xFFF

        # Ensure non-negative
        x = max(0, x)
        y = max(0, y)

        # Pack: y (bits 12-23) | x (bits 0-11)
        return (y << 12) | x

    def _encode_cpp_state(self) -> int:
        """
        Encode C-Stick and IR buttons into cppState format.

        Format (32 bits):
            Bits 0-7:   Fixed header byte (0x81)
            Bits 8-15:  IR button state (ZL/ZR)
            Bits 16-23: X coordinate (8-bit)
            Bits 24-31: Y coordinate (8-bit)

        CRITICAL: 45-DEGREE ROTATION
        The 3DS hardware expects C-Stick values rotated 45 degrees clockwise.
        This is a hardware quirk that must be compensated for.

        Rotation formula (counterclockwise to counteract hardware):
            x' = (x + y) / sqrt(2)
            y' = (y - x) / sqrt(2)

        Coordinate encoding:
            - Center position: 0x80 (128)
            - Full deflection: 0x80 ± CPP_BOUND (0x01 to 0xFF)

        Returns:
            32-bit cppState value
        """
        # Default value when centered with no IR buttons
        if (self._c_stick.x == 0.0 and self._c_stick.y == 0.0 and
            self._ir_buttons == IRButton.NONE):
            return 0x80800081

        # Apply 45-degree rotation
        # This rotates the coordinate system to match what the 3DS expects
        rx = self._c_stick.x
        ry = self._c_stick.y

        # Rotation matrix for -45 degrees (to counteract 3DS's +45 degree expectation):
        # [cos(-45)  -sin(-45)] = [√½   √½]
        # [sin(-45)   cos(-45)]   [-√½  √½]
        #
        # x' = x*cos(-45) - y*sin(-45) = x*√½ + y*√½ = (x + y) * √½
        # y' = x*sin(-45) + y*cos(-45) = -x*√½ + y*√½ = (y - x) * √½
        rotated_x = SQRT_1_2 * (rx + ry)
        rotated_y = SQRT_1_2 * (ry - rx)

        # Convert to 8-bit coordinate centered at 0x80
        x = int(rotated_x * CPP_BOUND + 0x80)
        y = int(rotated_y * CPP_BOUND + 0x80)

        # Clamp to valid 8-bit range
        if x >= 0xFF:
            x = 0x00 if rotated_x < 0 else 0xFF
        if y >= 0xFF:
            y = 0x00 if rotated_y < 0 else 0xFF

        x = max(0, min(0xFF, x))
        y = max(0, min(0xFF, y))

        # Pack: y (bits 24-31) | x (bits 16-23) | ir_buttons (bits 8-15) | 0x81 (bits 0-7)
        return (y << 24) | (x << 16) | (int(self._ir_buttons) << 8) | 0x81

    def _build_packet(self) -> bytes:
        """
        Build the complete 20-byte UDP packet.

        Packet layout:
            Bytes 0-3:   hidPad (button states)
            Bytes 4-7:   touchScreenState
            Bytes 8-11:  circlePadState
            Bytes 12-15: cppState (C-Stick + ZL/ZR)
            Bytes 16-19: interfaceButtons (Home/Power)

        All values are encoded as little-endian 32-bit unsigned integers.

        Returns:
            20-byte packet ready to send over UDP
        """
        hid_pad = self._encode_hid_pad()
        touch_screen = self._encode_touch_screen()
        circle_pad = self._encode_circle_pad()
        cpp_state = self._encode_cpp_state()
        interface_buttons = int(self._interface_buttons)

        # Pack as 5 little-endian 32-bit unsigned integers
        # '<' = little-endian, 'I' = unsigned 32-bit integer
        packet = struct.pack('<IIIII',
                           hid_pad,
                           touch_screen,
                           circle_pad,
                           cpp_state,
                           interface_buttons)

        return packet

    # =========================================================================
    # SENDING
    # =========================================================================

    def send_frame(self) -> None:
        """
        Send the current input state to the 3DS.

        This method:
        1. Encodes all current input state into a 20-byte packet
        2. Sends the packet via UDP to the configured 3DS IP/port

        Call this method after making any input changes to transmit them.
        For smooth input, call this at regular intervals (e.g., every 50ms).

        Note: UDP is connectionless and does not guarantee delivery.
        For responsive input, send frames frequently.
        """
        packet = self._build_packet()
        self._socket.sendto(packet, (self.ip_address, self.port))

    def close(self) -> None:
        """
        Close the UDP socket and release resources.

        Call this when done using the client.
        """
        # Send a final frame with all inputs released
        self.reset_all()
        try:
            self.send_frame()
        except:
            pass
        self._socket.close()

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures socket is closed."""
        self.close()
        return False


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def create_client(ip_address: str) -> InputRedirectionClient:
    """
    Create an InputRedirectionClient connected to the specified 3DS.

    Args:
        ip_address: IP address of the 3DS (e.g., "192.168.1.100")

    Returns:
        Configured InputRedirectionClient instance

    Example:
        client = create_client("192.168.1.100")
        client.press_button(Button.A)
        client.send_frame()
    """
    return InputRedirectionClient(ip_address)


# =============================================================================
# EXAMPLE USAGE / DEMO
# =============================================================================

if __name__ == "__main__":
    import time
    import sys

    # Example usage demonstrating all features
    print("Nintendo 3DS Input Redirection Client")
    print("=" * 40)

    # Get IP address from command line or use default
    if len(sys.argv) > 1:
        ip = sys.argv[1]
    else:
        ip = "192.168.1.100"  # Change this to your 3DS IP
        print(f"Usage: {sys.argv[0]} <3ds_ip_address>")
        print(f"Using default IP: {ip}")

    print(f"\nConnecting to 3DS at {ip}:{INPUT_REDIRECTION_PORT}")
    print("Press Ctrl+C to stop\n")

    try:
        # Using context manager ensures cleanup on exit
        with InputRedirectionClient(ip) as client:

            # Demo 1: Press A button
            print("Demo 1: Pressing A button...")
            client.press_button(Button.A)
            client.send_frame()
            time.sleep(0.5)

            client.release_button(Button.A)
            client.send_frame()
            time.sleep(0.3)

            # Demo 2: Press multiple buttons
            print("Demo 2: Pressing A + B together...")
            client.press_button(Button.A | Button.B)
            client.send_frame()
            time.sleep(0.5)

            client.release_all_buttons()
            client.send_frame()
            time.sleep(0.3)

            # Demo 3: Move Circle Pad
            print("Demo 3: Moving Circle Pad in a circle...")
            for angle in range(0, 360, 30):
                rad = math.radians(angle)
                x = math.cos(rad)
                y = math.sin(rad)
                client.set_circle_pad(x, y)
                client.send_frame()
                time.sleep(0.1)

            client.reset_circle_pad()
            client.send_frame()
            time.sleep(0.3)

            # Demo 4: Move C-Stick
            print("Demo 4: Moving C-Stick...")
            client.set_c_stick(1.0, 0.0)  # Right
            client.send_frame()
            time.sleep(0.3)

            client.set_c_stick(0.0, 1.0)  # Up
            client.send_frame()
            time.sleep(0.3)

            client.reset_c_stick()
            client.send_frame()
            time.sleep(0.3)

            # Demo 5: Touch screen
            print("Demo 5: Touching screen center...")
            client.touch(160, 120)  # Center of 320x240 screen
            client.send_frame()
            time.sleep(0.5)

            client.release_touch()
            client.send_frame()
            time.sleep(0.3)

            # Demo 6: ZL/ZR buttons
            print("Demo 6: Pressing ZL and ZR...")
            client.press_ir_button(IRButton.ZL)
            client.send_frame()
            time.sleep(0.3)

            client.press_ir_button(IRButton.ZR)
            client.send_frame()
            time.sleep(0.3)

            client.set_ir_buttons(IRButton.NONE)
            client.send_frame()

            print("\nDemo complete! All inputs released.")

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)
