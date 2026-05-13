"""Glasgow applet: UART-triggered glitch controller CLI."""

import argparse
import asyncio
import logging
import sys
import time

from glasgow.applet import GlasgowAppletV2, GlasgowAppletError

from .gateware import S_DONE
from .interface import GlitchInterface


class GlitchApplet(GlasgowAppletV2):
    logger = logging.getLogger(__name__)
    help = "trigger a glitch on a UART byte pattern"
    description = """
    Monitor a UART RX line for a configurable byte sequence and, when armed,
    fire a precisely-timed pulse on a trigger pin after a configurable delay.

    Useful as the controller for voltage, clock, or EMFI glitching campaigns.
    The pattern match, delay, and pulse generation all happen in FPGA gateware
    so the trigger latency is deterministic to within a few system clock
    cycles (~21 ns on Glasgow C3).
    """

    required_revision = "C0"

    # ------------------------------------------------------------------
    # Build: pins and voltage. Runs once at bitstream-build time.
    # ------------------------------------------------------------------

    @classmethod
    def add_build_arguments(cls, parser, access):
        access.add_voltage_argument(parser)
        access.add_pins_argument(parser, "rx", default=True, required=True,
            help="UART RX pin to monitor (target TX)")
        access.add_pins_argument(parser, "trigger", default=True, required=True,
            help="glitch trigger output pin")
        access.add_pins_argument(parser, "reset", default=None, required=False,
            help="optional target reset output pin (active-low, push-pull). "
                 "Used by boot-time EMFI campaigns to reset the target and "
                 "trigger on its boot-banner UART output. Omit for plain "
                 "UART-triggered glitch operation.")

    def build(self, args):
        with self.assembly.add_applet(self):
            self.assembly.use_voltage(args.voltage)
            self.glitch_iface = GlitchInterface(
                self.logger, self.assembly,
                rx=args.rx, trigger=args.trigger,
                reset=getattr(args, "reset", None))

    # ------------------------------------------------------------------
    # Setup: runtime config that doesn't vary per-shot (baud, polarity).
    # ------------------------------------------------------------------

    @classmethod
    def add_setup_arguments(cls, parser):
        parser.add_argument(
            "-b", "--baud", metavar="RATE", type=int, default=115200,
            help="UART baud rate (default: %(default)s)")
        parser.add_argument(
            "--active-low", action="store_true", default=False,
            help="invert trigger polarity (idle high, pulse low) — common for "
                 "P-channel MOSFET crowbar drivers")
        parser.add_argument(
            "--open-drain", action="store_true", default=False,
            help="emulate open-drain output: drive only during the pulse, "
                 "tri-state at idle. Use when the receiver has its own pull-up "
                 "(e.g. ChipShouter active-low HW TRIG) so a CMOS push-pull "
                 "drive doesn't contend with it.")

    async def setup(self, args):
        await self.glitch_iface.set_baud(args.baud)
        await self.glitch_iface.set_polarity(args.active_low)
        await self.glitch_iface.set_open_drain(args.open_drain)

    # ------------------------------------------------------------------
    # Run: per-invocation operations.
    # ------------------------------------------------------------------

    @classmethod
    def add_run_arguments(cls, parser):
        sub = parser.add_subparsers(dest="operation", metavar="OPERATION", required=True)

        p_fire = sub.add_parser("fire",
            help="arm once, wait for pattern, fire glitch, report result")
        _add_pattern_argument(p_fire)
        _add_timing_arguments(p_fire)
        p_fire.add_argument(
            "-t", "--timeout", metavar="SEC", type=float, default=10.0,
            help="give up waiting for match after SEC seconds (default: %(default)s)")
        p_fire.add_argument(
            "--log-rx", action="store_true", default=False,
            help="stream received UART bytes to stdout while armed")

        sub.add_parser("monitor",
            help="passive UART passthrough to stdout (no triggering)")

    async def run(self, args):
        match args.operation:
            case "fire":
                await self._run_fire(args)
            case "monitor":
                await self._run_monitor(args)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    async def _run_fire(self, args):
        pattern   = _parse_pattern(args)
        sys_clk   = self.glitch_iface.sys_clk_period
        delay_cyc = _parse_delay(args, sys_clk)
        pulse_cyc = _parse_pulse(args, sys_clk)

        await self.glitch_iface.set_pattern(pattern)
        await self.glitch_iface.set_delay_cycles(delay_cyc)
        await self.glitch_iface.set_pulse_cycles(pulse_cyc)

        self.logger.info("pattern: %r (%d bytes, hex %s)",
            pattern, len(pattern), pattern.hex())
        self.logger.info("delay:   %d cycles (%.3f us)",
            delay_cyc, delay_cyc * sys_clk * 1e6)
        self.logger.info("pulse:   %d cycles (%.3f us)",
            pulse_cyc, pulse_cyc * sys_clk * 1e6)

        if args.log_rx:
            fired = await self._fire_with_log(args.timeout)
        else:
            self.logger.info("armed; waiting for match...")
            fired = await self.glitch_iface.fire_once(timeout=args.timeout)

        matches = await self.glitch_iface.get_match_count()
        errors  = await self.glitch_iface.get_rx_errors()
        if fired:
            self.logger.info("FIRED (match_count=%d, rx_errors=%d)", matches, errors)
        else:
            state = await self.glitch_iface.get_state_name()
            self.logger.warning("timeout; state=%s match_count=%d rx_errors=%d",
                state, matches, errors)
            raise GlasgowAppletError("trigger did not fire within timeout")

    async def _fire_with_log(self, timeout):
        """`fire_once` but also stream RX bytes to stdout until DONE/timeout."""
        await self.glitch_iface.disarm()
        await self.glitch_iface.arm()
        self.logger.info("armed; streaming RX (stdout) until match...")
        deadline = time.monotonic() + timeout if timeout is not None else None
        fired = False
        try:
            while True:
                data = await self.glitch_iface.read_rx_available()
                if data:
                    sys.stdout.buffer.write(bytes(data))
                    sys.stdout.buffer.flush()
                if (await self.glitch_iface.get_state()) == S_DONE:
                    fired = True
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.005)
        finally:
            await self.glitch_iface.disarm()
        return fired

    async def _run_monitor(self, args):
        self.logger.info("UART passthrough (Ctrl-C to stop)...")
        while True:
            data = await self.glitch_iface.read_rx(1)         # block for ≥1 byte
            data = bytes(data) + bytes(await self.glitch_iface.read_rx_available())
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()


# ----------------------------------------------------------------------
# Argparse helpers
# ----------------------------------------------------------------------

def _add_pattern_argument(parser):
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "-p", "--pattern", metavar="HEX", type=_parse_hex_pattern, default=None,
        help="byte pattern, hex-encoded (e.g. '48494f' → b'HIO'); "
             "spaces and ':' separators accepted")
    grp.add_argument(
        "-s", "--pattern-string", metavar="STR", type=_parse_str_pattern,
        dest="pattern_string", default=None,
        help=r"byte pattern as a string, with \n \r \xNN escapes")


def _add_timing_arguments(parser):
    dg = parser.add_mutually_exclusive_group(required=True)
    dg.add_argument("--delay-cyc", type=int,   metavar="N",  help="post-match delay in cycles")
    dg.add_argument("--delay-ns",  type=float, metavar="NS", help="post-match delay in ns")
    dg.add_argument("--delay-us",  type=float, metavar="US", help="post-match delay in us")
    dg.add_argument("--delay-ms",  type=float, metavar="MS", help="post-match delay in ms")

    pg = parser.add_mutually_exclusive_group(required=True)
    pg.add_argument("--pulse-cyc", type=int,   metavar="N",  help="pulse width in cycles")
    pg.add_argument("--pulse-ns",  type=float, metavar="NS", help="pulse width in ns")
    pg.add_argument("--pulse-us",  type=float, metavar="US", help="pulse width in us")
    pg.add_argument("--pulse-ms",  type=float, metavar="MS", help="pulse width in ms")


def _parse_hex_pattern(s):
    s = s.replace(" ", "").replace(":", "")
    try:
        data = bytes.fromhex(s)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid hex: {e}")
    if not data:
        raise argparse.ArgumentTypeError("pattern cannot be empty")
    return data


def _parse_str_pattern(s):
    # Decode common escapes (\n, \r, \xNN, …) without requiring the user to
    # quote-escape them in their shell.
    try:
        data = s.encode("latin-1").decode("unicode_escape").encode("latin-1")
    except (UnicodeDecodeError, UnicodeEncodeError) as e:
        raise argparse.ArgumentTypeError(f"invalid pattern string: {e}")
    if not data:
        raise argparse.ArgumentTypeError("pattern cannot be empty")
    return data


def _parse_pattern(args):
    if args.pattern is not None:
        return args.pattern
    if args.pattern_string is not None:
        return args.pattern_string
    raise GlasgowAppletError("no pattern specified")   # argparse should prevent this


def _seconds_to_cycles(seconds, sys_clk_period):
    cyc = round(seconds / sys_clk_period)
    return cyc


def _parse_delay(args, sys_clk_period):
    if args.delay_cyc is not None: return args.delay_cyc
    if args.delay_ns  is not None: return _seconds_to_cycles(args.delay_ns * 1e-9, sys_clk_period)
    if args.delay_us  is not None: return _seconds_to_cycles(args.delay_us * 1e-6, sys_clk_period)
    if args.delay_ms  is not None: return _seconds_to_cycles(args.delay_ms * 1e-3, sys_clk_period)
    raise GlasgowAppletError("no delay specified")


def _parse_pulse(args, sys_clk_period):
    if args.pulse_cyc is not None: return args.pulse_cyc
    if args.pulse_ns  is not None: return _seconds_to_cycles(args.pulse_ns * 1e-9, sys_clk_period)
    if args.pulse_us  is not None: return _seconds_to_cycles(args.pulse_us * 1e-6, sys_clk_period)
    if args.pulse_ms  is not None: return _seconds_to_cycles(args.pulse_ms * 1e-3, sys_clk_period)
    raise GlasgowAppletError("no pulse width specified")
