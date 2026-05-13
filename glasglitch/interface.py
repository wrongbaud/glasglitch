"""Host-side wrapper around GlitchComponent.

Binds the gateware's ports to host-visible registers and pipes, and exposes a
high-level async API: configure pattern / timing / polarity, arm, wait, read
back the UART passthrough.
"""

import asyncio
import logging
import time
from typing import Optional

from glasgow.abstract import AbstractAssembly, GlasgowPin
from glasgow.applet import GlasgowAppletError

from .gateware import (
    GlitchComponent, MAX_PATTERN_LEN,
    S_IDLE, S_ARMED, S_DELAY, S_PULSE, S_DONE,
)


STATE_NAMES = {
    S_IDLE:  "IDLE",
    S_ARMED: "ARMED",
    S_DELAY: "DELAY",
    S_PULSE: "PULSE",
    S_DONE:  "DONE",
}


class GlitchInterface:
    """Runtime control surface for the UART-triggered glitch generator."""

    def __init__(self, logger: logging.Logger, assembly: AbstractAssembly, *,
                 rx: GlasgowPin, trigger: GlasgowPin,
                 reset: Optional[GlasgowPin] = None):
        self._logger = logger
        self._sys_clk_period = assembly.sys_clk_period
        self._has_reset = reset is not None

        # UART RX idles high; pull it high so an unconnected input doesn't
        # continuously register start bits. The reset pin (when used) is
        # driven push-pull from the gateware so we don't need a pull.
        port_kwargs = {"rx": rx, "trigger": trigger}
        if reset is not None:
            port_kwargs["reset"] = reset
        ports = assembly.add_port_group(**port_kwargs)
        assembly.use_pulls({rx: "high"})

        component = assembly.add_submodule(
            GlitchComponent(ports, has_reset=self._has_reset))

        # Config registers (host writes).
        self._arm         = assembly.add_rw_register(component.arm)
        self._polarity    = assembly.add_rw_register(component.polarity)
        self._open_drain  = assembly.add_rw_register(component.open_drain)
        self._pattern     = assembly.add_rw_register(component.pattern)
        self._pattern_len = assembly.add_rw_register(component.pattern_len)
        self._manual_cyc  = assembly.add_rw_register(component.manual_cyc)
        self._delay_cyc   = assembly.add_rw_register(component.delay_cyc)
        self._pulse_cyc   = assembly.add_rw_register(component.pulse_cyc)
        # reset_assert register always exists at the gateware level; we only
        # bind a host-visible register for it when a reset pin was wired in.
        # Writes are no-ops when has_reset=False (no physical pin to drive).
        self._reset_assert = (
            assembly.add_rw_register(component.reset_assert)
            if self._has_reset else None)

        # Status registers (host reads).
        self._state       = assembly.add_ro_register(component.state)
        self._match_count = assembly.add_ro_register(component.match_count)
        self._rx_errors   = assembly.add_ro_register(component.rx_errors)

        # RX passthrough pipe (host-readable).
        self._rx_pipe = assembly.add_in_pipe(
            component.rx_stream, in_flush=component.rx_flush)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    async def set_pattern(self, pattern: bytes) -> None:
        """Set the UART byte sequence to match. Length must be 1..MAX_PATTERN_LEN."""
        if not 1 <= len(pattern) <= MAX_PATTERN_LEN:
            raise GlasgowAppletError(
                f"pattern must be 1..{MAX_PATTERN_LEN} bytes; got {len(pattern)}")
        # Packing: first-received byte goes into the highest occupied slot,
        # last-received into slot 0. Big-endian into an integer gives exactly
        # that, because slot j occupies bits [j*8, (j+1)*8).
        await self._pattern.set(int.from_bytes(pattern, "big"))
        await self._pattern_len.set(len(pattern))
        self._logger.debug("pattern set (%d bytes): %s", len(pattern), pattern.hex())

    async def set_baud(self, baud: int) -> None:
        """Set UART baud rate in bits per second."""
        cyc = round(1 / (baud * self._sys_clk_period))
        if cyc < 2:
            raise GlasgowAppletError(f"baud rate {baud} is too high")
        if cyc >= (1 << 20):
            raise GlasgowAppletError(f"baud rate {baud} is too low")
        await self._manual_cyc.set(cyc)
        self._logger.debug("baud set: %d (bit_cyc=%d)", baud, cyc)

    async def set_delay_cycles(self, cycles: int) -> None:
        if not 0 <= cycles < (1 << 32):
            raise GlasgowAppletError(f"delay {cycles} cycles out of range")
        await self._delay_cyc.set(cycles)

    async def set_delay_seconds(self, seconds: float) -> None:
        await self.set_delay_cycles(round(seconds / self._sys_clk_period))

    async def set_pulse_cycles(self, cycles: int) -> None:
        if not 1 <= cycles < (1 << 32):
            raise GlasgowAppletError(f"pulse width {cycles} cycles out of range")
        await self._pulse_cyc.set(cycles)

    async def set_pulse_seconds(self, seconds: float) -> None:
        await self.set_pulse_cycles(round(seconds / self._sys_clk_period))

    async def set_polarity(self, active_low: bool) -> None:
        """0 / False = active-high pulse (idle low). 1 / True = active-low (idle high)."""
        await self._polarity.set(int(bool(active_low)))

    async def set_open_drain(self, open_drain: bool) -> None:
        """0 / False = push-pull (always drive). 1 / True = emulate open-drain
        (drive only during pulse, tri-state at idle). Useful when the receiver
        has its own pull-up and would contend with an active CMOS drive — e.g.
        the ChipShouter active-low HW TRIG input."""
        await self._open_drain.set(int(bool(open_drain)))

    @property
    def sys_clk_period(self) -> float:
        return self._sys_clk_period

    @property
    def has_reset(self) -> bool:
        return self._has_reset

    # ------------------------------------------------------------------
    # Reset pin (optional — only available when the applet was built
    # with --reset PIN). Host-timed pulse: deterministic to within
    # asyncio.sleep granularity (~1 ms), which is fine for typical reset
    # synchronizer requirements (<< 100 µs).
    # ------------------------------------------------------------------

    async def pulse_reset(self, duration_us: int) -> None:
        """Drive the reset pin low for `duration_us`, release, return.

        Blocking: this call returns only after the pin has been released,
        so the caller can assume the target is just starting its boot
        cycle when this returns. The gateware register controls the pin
        directly; timing is host-clock driven via asyncio.sleep.

        Raises if no reset pin was configured at build time.
        """
        if self._reset_assert is None:
            raise GlasgowAppletError(
                "pulse_reset called but no reset pin was configured at "
                "applet build time — pass --reset PIN to the applet")
        await self._reset_assert.set(1)
        await asyncio.sleep(duration_us / 1_000_000)
        await self._reset_assert.set(0)

    async def assert_reset(self) -> None:
        """Hold reset low indefinitely. Use for setup sequences where you
        want to keep the target in reset while configuring other things;
        release with `release_reset`. `pulse_reset` is preferred for
        normal use."""
        if self._reset_assert is None:
            raise GlasgowAppletError(
                "assert_reset called but no reset pin was configured")
        await self._reset_assert.set(1)

    async def release_reset(self) -> None:
        """Release a previously-asserted reset (drive pin high)."""
        if self._reset_assert is None:
            raise GlasgowAppletError(
                "release_reset called but no reset pin was configured")
        await self._reset_assert.set(0)

    # ------------------------------------------------------------------
    # Arm / poll / fire
    # ------------------------------------------------------------------

    async def arm(self) -> None:
        await self._arm.set(1)

    async def disarm(self) -> None:
        await self._arm.set(0)

    async def get_state(self) -> int:
        return await self._state

    async def get_state_name(self) -> str:
        return STATE_NAMES.get(await self._state, "?")

    async def get_match_count(self) -> int:
        return await self._match_count

    async def get_rx_errors(self) -> int:
        return await self._rx_errors

    async def wait_done(self, *, timeout: Optional[float] = None,
                        poll_interval: float = 0.001) -> bool:
        """Poll until the FSM reaches DONE. Returns True if it did, False on
        timeout. Does not touch `arm`."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if (await self._state) == S_DONE:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            await asyncio.sleep(poll_interval)

    async def fire_once(self, *, timeout: Optional[float] = 5.0,
                        poll_interval: float = 0.001) -> bool:
        """Full one-shot cycle: disarm → arm → wait for DONE (or timeout) →
        disarm. Returns True if the trigger fired within the timeout."""
        await self.disarm()                             # force IDLE
        await self.arm()                                # IDLE → ARMED
        fired = await self.wait_done(timeout=timeout, poll_interval=poll_interval)
        await self.disarm()                             # back to IDLE
        return fired

    # ------------------------------------------------------------------
    # RX passthrough
    # ------------------------------------------------------------------

    async def read_rx_available(self) -> memoryview:
        """Drain any buffered RX bytes. Empty if nothing is waiting."""
        n = self._rx_pipe.readable
        if n == 0:
            return memoryview(b"")
        return await self._rx_pipe.recv(n)

    async def read_rx(self, n: int) -> memoryview:
        """Block until `n` bytes have been received."""
        return await self._rx_pipe.recv(n)
