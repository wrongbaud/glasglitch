"""Simulation tests for the glitch applet.

Run with::

    uv run python -m unittest glasglitch.test -v
"""

import unittest

from glasgow.applet import GlasgowAppletV2TestCase, applet_v2_simulation_test

from .applet import GlitchApplet
from .gateware import (S_IDLE, S_ARMED, S_DELAY, S_PULSE, S_DELAY2, S_PULSE2,
                       S_DELAY3, S_PULSE3, S_DONE)


async def _drive_uart_byte(ctx, pin, byte, bit_cyc):
    """Drive a single 8-N-1 UART byte onto `pin.i`."""
    ctx.set(pin.i, 0)                              # start bit
    await ctx.tick().repeat(bit_cyc)
    for i in range(8):                             # 8 data bits, LSB first
        ctx.set(pin.i, (byte >> i) & 1)
        await ctx.tick().repeat(bit_cyc)
    ctx.set(pin.i, 1)                              # stop bit + idle
    await ctx.tick().repeat(bit_cyc)


def _prepare_idle_rx(self, assembly):
    """Idle the RX pin high at tick 0, before the main testbench wakes up.

    Without this, the pin defaults to 0 in simulation (pulls aren't modeled),
    and the UART sees a spurious start bit before we've configured anything.
    """
    rx_pin = assembly.get_pin("A0")
    async def idle(ctx):
        ctx.set(rx_pin.i, 1)
    assembly.add_testbench(idle, background=True)


class GlitchAppletTestCase(GlasgowAppletV2TestCase, applet=GlitchApplet):

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_match_fires_trigger(self, applet, ctx):
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")
        trig  = asm.get_pin("A1")

        ctx.set(rx.i, 1)                           # UART idle

        PATTERN   = b"OK"
        DELAY_CYC = 20
        PULSE_CYC = 30
        await iface.set_pattern(PATTERN)
        await iface.set_delay_cycles(DELAY_CYC)
        await iface.set_pulse_cycles(PULSE_CYC)
        await iface.arm()
        await ctx.tick()                           # let FSM see arm=1

        self.assertEqual(await iface.get_state(), S_ARMED)
        self.assertEqual(ctx.get(trig.o), 0)       # idle: active-high, pulse=0

        bit_cyc = round(1 / (9600 * asm.sys_clk_period))

        # Non-matching byte first — proves the matcher doesn't fire spuriously.
        await _drive_uart_byte(ctx, rx, ord("x"), bit_cyc)
        self.assertEqual(await iface.get_state(), S_ARMED)
        self.assertEqual(await iface.get_match_count(), 0)

        # Now drive the pattern bytes.
        for byte in PATTERN:
            await _drive_uart_byte(ctx, rx, byte, bit_cyc)

        # After the last byte's stop bit, the UART RX fires rx_rdy; one cycle
        # later the matcher sees the update; then DELAY_CYC + PULSE_CYC cycles
        # before DONE. Wait a generous margin.
        await ctx.tick().repeat(DELAY_CYC + PULSE_CYC + 50)

        self.assertEqual(await iface.get_state(), S_DONE)
        self.assertEqual(await iface.get_match_count(), 1)
        self.assertEqual(await iface.get_rx_errors(), 0)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_no_match_no_fire(self, applet, ctx):
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")

        ctx.set(rx.i, 1)

        await iface.set_pattern(b"OK")
        await iface.set_delay_cycles(10)
        await iface.set_pulse_cycles(10)
        await iface.arm()
        await ctx.tick()

        bit_cyc = round(1 / (9600 * asm.sys_clk_period))
        for byte in b"HELLO":                      # never contains 'OK'
            await _drive_uart_byte(ctx, rx, byte, bit_cyc)

        self.assertEqual(await iface.get_state(), S_ARMED)
        self.assertEqual(await iface.get_match_count(), 0)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_trigger_pin_pulses(self, applet, ctx):
        """Verify trigger pin is asserted for `pulse_cyc` cycles, active-high by default."""
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")
        trig  = asm.get_pin("A1")

        # Use timings longer than a byte (~1040 cycles) so PULSE is still
        # happening when the main testbench returns from driving.
        DELAY_CYC = 2000
        PULSE_CYC = 500
        await iface.set_pattern(b"X")
        await iface.set_delay_cycles(DELAY_CYC)
        await iface.set_pulse_cycles(PULSE_CYC)
        await iface.arm()
        await ctx.tick()

        self.assertEqual(ctx.get(trig.o), 0)       # idle: active-high, pulse=0
        bit_cyc = round(1 / (9600 * asm.sys_clk_period))
        await _drive_uart_byte(ctx, rx, ord("X"), bit_cyc)

        # Walk tick-by-tick counting high cycles, bail at DONE.
        high_cycles = 0
        for _ in range(DELAY_CYC + PULSE_CYC + 100):
            await ctx.tick()
            if ctx.get(trig.o):
                high_cycles += 1
            if (await iface.get_state()) == S_DONE:
                break
        self.assertEqual(await iface.get_state(), S_DONE)
        self.assertGreaterEqual(high_cycles, PULSE_CYC)
        self.assertLessEqual(high_cycles, PULSE_CYC + 2)
        self.assertEqual(ctx.get(trig.o), 0)       # back to idle

    @applet_v2_simulation_test(prepare=_prepare_idle_rx,
        args="--rx A0 --trigger A1 --baud 9600 --active-low")
    async def test_active_low_polarity(self, applet, ctx):
        """Active-low mode: pin idles high, pulses low."""
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")
        trig  = asm.get_pin("A1")

        DELAY_CYC = 2000
        PULSE_CYC = 500
        await iface.set_pattern(b"X")
        await iface.set_delay_cycles(DELAY_CYC)
        await iface.set_pulse_cycles(PULSE_CYC)
        await iface.arm()
        await ctx.tick()

        self.assertEqual(ctx.get(trig.o), 1)       # idle: active-low → high
        bit_cyc = round(1 / (9600 * asm.sys_clk_period))
        await _drive_uart_byte(ctx, rx, ord("X"), bit_cyc)

        low_cycles = 0
        for _ in range(DELAY_CYC + PULSE_CYC + 100):
            await ctx.tick()
            if ctx.get(trig.o) == 0:
                low_cycles += 1
            if (await iface.get_state()) == S_DONE:
                break
        self.assertEqual(await iface.get_state(), S_DONE)
        self.assertGreaterEqual(low_cycles, PULSE_CYC)
        self.assertEqual(ctx.get(trig.o), 1)       # back to idle (high)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx,
        args="--rx A0 --trigger A1 --reset A2 --baud 9600")
    async def test_reset_pin_pulses(self, applet, ctx):
        """Verify the optional reset pin: idle high, drives low when
        reset_assert is set, returns high when cleared. Driven push-pull
        (oe always asserted), active-low at the pad."""
        iface = applet.glitch_iface
        asm   = applet.assembly
        rst   = asm.get_pin("A2")

        self.assertTrue(iface.has_reset)

        # Idle state: high (reset_assert=0 → ~reset_assert=1 at pad)
        await ctx.tick()
        self.assertEqual(ctx.get(rst.o), 1)
        self.assertEqual(ctx.get(rst.oe), 1)

        # Drive a 100-cycle low pulse via the register directly (we don't
        # use pulse_reset() here because asyncio.sleep doesn't compose
        # with the simulation clock). Time-domain correctness of the
        # asyncio.sleep wrapping is covered by the hardware bring-up test
        # in the harness.
        await iface.assert_reset()
        for _ in range(100):
            await ctx.tick()
            self.assertEqual(ctx.get(rst.o), 0)

        # Release: pin goes high
        await iface.release_reset()
        await ctx.tick()
        self.assertEqual(ctx.get(rst.o), 1)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_no_reset_pin_when_not_configured(self, applet, ctx):
        """If --reset is omitted at build time, the interface reports
        has_reset=False and the reset methods raise."""
        iface = applet.glitch_iface
        self.assertFalse(iface.has_reset)
        from glasgow.applet import GlasgowAppletError
        with self.assertRaises(GlasgowAppletError):
            await iface.pulse_reset(1000)
        with self.assertRaises(GlasgowAppletError):
            await iface.assert_reset()

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_double_pulse_chains_through_delay2(self, applet, ctx):
        """With pulse2_cyc > 0, FSM is PULSE → DELAY2 → PULSE2 → DONE.
        Trigger pin asserts during both PULSE and PULSE2, idles between
        them for DELAY2 cycles."""
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")
        trig  = asm.get_pin("A1")

        DELAY_CYC  = 200
        PULSE_CYC  = 100
        DELAY2_CYC = 300
        PULSE2_CYC = 150
        await iface.set_pattern(b"X")
        await iface.set_delay_cycles(DELAY_CYC)
        await iface.set_pulse_cycles(PULSE_CYC)
        await iface.set_delay2_cycles(DELAY2_CYC)
        await iface.set_pulse2_cycles(PULSE2_CYC)
        await iface.arm()
        await ctx.tick()

        bit_cyc = round(1 / (9600 * asm.sys_clk_period))
        await _drive_uart_byte(ctx, rx, ord("X"), bit_cyc)

        # Walk the FSM tick-by-tick. Record state transitions and
        # count high cycles within each PULSE / PULSE2 phase to verify
        # the pin really asserts twice with an idle gap.
        states_seen = []
        pulse1_high = 0
        pulse2_high = 0
        delay2_low  = 0
        budget = DELAY_CYC + PULSE_CYC + DELAY2_CYC + PULSE2_CYC + 100
        for _ in range(budget):
            await ctx.tick()
            st = await iface.get_state()
            if not states_seen or states_seen[-1] != st:
                states_seen.append(st)
            if st == S_PULSE:
                if ctx.get(trig.o):
                    pulse1_high += 1
            elif st == S_DELAY2:
                if ctx.get(trig.o) == 0:
                    delay2_low += 1
            elif st == S_PULSE2:
                if ctx.get(trig.o):
                    pulse2_high += 1
            if st == S_DONE:
                break

        self.assertEqual(await iface.get_state(), S_DONE)
        # Verify the FSM visited both delay/pulse phases in order.
        # (filter out IDLE/ARMED if present at the very start)
        seen_after_armed = [s for s in states_seen
                            if s not in (S_IDLE, S_ARMED)]
        self.assertEqual(
            seen_after_armed,
            [S_DELAY, S_PULSE, S_DELAY2, S_PULSE2, S_DONE],
            f"unexpected state sequence: {seen_after_armed}")
        # Pulse-width sanity: both pulses asserted for ~their configured cycles.
        # Allow +/- 2 cycles for sampling-edge boundary effects.
        self.assertGreaterEqual(pulse1_high, PULSE_CYC - 2)
        self.assertLessEqual(pulse1_high,    PULSE_CYC + 2)
        self.assertGreaterEqual(pulse2_high, PULSE2_CYC - 2)
        self.assertLessEqual(pulse2_high,    PULSE2_CYC + 2)
        # DELAY2 idled the pin low for at least most of its window.
        self.assertGreaterEqual(delay2_low, DELAY2_CYC - 2)
        self.assertEqual(ctx.get(trig.o), 0)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_triple_pulse_chains_through_delay3(self, applet, ctx):
        """With pulse3_cyc > 0, FSM is PULSE → DELAY2 → PULSE2 → DELAY3 → PULSE3 →
        DONE. Trigger asserts during all three pulses, idling between them."""
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")
        trig  = asm.get_pin("A1")

        DELAY_CYC, PULSE_CYC = 100, 60
        DELAY2_CYC, PULSE2_CYC = 120, 60
        DELAY3_CYC, PULSE3_CYC = 140, 60
        await iface.set_pattern(b"X")
        await iface.set_delay_cycles(DELAY_CYC)
        await iface.set_pulse_cycles(PULSE_CYC)
        await iface.set_delay2_cycles(DELAY2_CYC)
        await iface.set_pulse2_cycles(PULSE2_CYC)
        await iface.set_delay3_cycles(DELAY3_CYC)
        await iface.set_pulse3_cycles(PULSE3_CYC)
        await iface.arm()
        await ctx.tick()

        bit_cyc = round(1 / (9600 * asm.sys_clk_period))
        await _drive_uart_byte(ctx, rx, ord("X"), bit_cyc)

        states_seen = []
        pulse3_high = 0
        delay3_low  = 0
        budget = (DELAY_CYC + PULSE_CYC + DELAY2_CYC + PULSE2_CYC
                  + DELAY3_CYC + PULSE3_CYC + 100)
        for _ in range(budget):
            await ctx.tick()
            st = await iface.get_state()
            if not states_seen or states_seen[-1] != st:
                states_seen.append(st)
            if st == S_DELAY3 and ctx.get(trig.o) == 0:
                delay3_low += 1
            elif st == S_PULSE3 and ctx.get(trig.o):
                pulse3_high += 1
            if st == S_DONE:
                break

        self.assertEqual(await iface.get_state(), S_DONE)
        seen_after_armed = [s for s in states_seen if s not in (S_IDLE, S_ARMED)]
        self.assertEqual(
            seen_after_armed,
            [S_DELAY, S_PULSE, S_DELAY2, S_PULSE2, S_DELAY3, S_PULSE3, S_DONE],
            f"unexpected state sequence: {seen_after_armed}")
        self.assertGreaterEqual(pulse3_high, PULSE3_CYC - 2)
        self.assertLessEqual(pulse3_high,    PULSE3_CYC + 2)
        self.assertGreaterEqual(delay3_low, DELAY3_CYC - 2)
        self.assertEqual(ctx.get(trig.o), 0)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_pulse2_cyc_zero_preserves_single_pulse(self, applet, ctx):
        """pulse2_cyc=0 must keep the original single-pulse FSM: PULSE → DONE
        directly, never entering DELAY2 / PULSE2."""
        iface = applet.glitch_iface
        asm   = applet.assembly
        rx    = asm.get_pin("A0")

        await iface.set_pattern(b"X")
        await iface.set_delay_cycles(50)
        await iface.set_pulse_cycles(50)
        # delay2 set to a non-zero value; with pulse2=0 it must be ignored.
        await iface.set_delay2_cycles(9999)
        await iface.set_pulse2_cycles(0)
        await iface.arm()
        await ctx.tick()

        bit_cyc = round(1 / (9600 * asm.sys_clk_period))
        await _drive_uart_byte(ctx, rx, ord("X"), bit_cyc)

        states_seen = set()
        for _ in range(50 + 50 + 100):
            await ctx.tick()
            st = await iface.get_state()
            states_seen.add(st)
            if st == S_DONE:
                break
        self.assertEqual(await iface.get_state(), S_DONE)
        self.assertNotIn(S_DELAY2, states_seen)
        self.assertNotIn(S_PULSE2, states_seen)

    @applet_v2_simulation_test(prepare=_prepare_idle_rx, args="--rx A0 --trigger A1 --baud 9600")
    async def test_disarm_returns_to_idle(self, applet, ctx):
        iface = applet.glitch_iface
        rx    = applet.assembly.get_pin("A0")
        ctx.set(rx.i, 1)

        await iface.set_pattern(b"A")
        await iface.set_delay_cycles(10)
        await iface.set_pulse_cycles(10)
        await iface.arm()
        await ctx.tick()
        self.assertEqual(await iface.get_state(), S_ARMED)

        await iface.disarm()
        await ctx.tick()
        self.assertEqual(await iface.get_state(), S_IDLE)


if __name__ == "__main__":
    unittest.main()
