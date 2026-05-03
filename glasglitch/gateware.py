"""Gateware: UART RX → sliding-window pattern matcher → one-shot glitch FSM.

Runs on the Glasgow FPGA. Watches a UART RX line, and when a configured byte
pattern is received while armed, waits a programmable delay, then drives a
dedicated trigger pin high (or low, per `polarity`) for a programmable pulse
width.
"""

from amaranth import *
from amaranth.lib import wiring, stream, io
from amaranth.lib.wiring import In, Out

from glasgow.gateware.uart import UART


MAX_PATTERN_LEN = 16


# Glitch FSM states, exposed as Out(3) so the host can poll progress.
S_IDLE  = 0
S_ARMED = 1
S_DELAY = 2
S_PULSE = 3
S_DONE  = 4


class GlitchComponent(wiring.Component):
    """UART-triggered one-shot glitch pulse generator.

    Host protocol:
      1. Configure pattern / pattern_len / manual_cyc / delay_cyc / pulse_cyc /
         polarity via their registers.
      2. Write arm=1. FSM transitions IDLE → ARMED.
      3. Poll `state`. When it reaches DONE, the glitch has fired.
      4. Write arm=0 to return to IDLE. Re-arm by writing 1 again.
      5. `arm=0` at any point aborts and returns to IDLE.
    """

    # --- Host-controlled (In) ---
    arm:         In(1)
    polarity:    In(1)                       # 0 = active-high pulse, 1 = active-low
    open_drain:  In(1)                       # 1 = emulate open-drain (drive only
                                             # during pulse, tristate at idle); 0 =
                                             # push-pull (always drive). Used to
                                             # interface cleanly with the
                                             # ChipShouter active-low HW TRIG input
                                             # which has its own pull-up.
    pattern:     In(MAX_PATTERN_LEN * 8)     # packed: bytes[0] at MSB, see README
    pattern_len: In(range(MAX_PATTERN_LEN + 1))
    manual_cyc:  In(20)                      # UART bit period in sys clocks
    delay_cyc:   In(32)                      # cycles between match and pulse rise
    pulse_cyc:   In(32)                      # pulse width in cycles

    # --- Host-observable (Out) ---
    state:       Out(3)
    match_count: Out(16)                     # increments each time a match fires
    rx_errors:   Out(16)                     # UART framing/parity/overflow count

    # --- UART passthrough to host (best-effort) ---
    rx_stream:   Out(stream.Signature(8))
    rx_flush:    Out(1)

    def __init__(self, ports):
        # `ports` is a port group with `.rx` (UART input) and `.trigger`
        # (glitch output). `.tx` may be present but is unused.
        self.ports = ports
        super().__init__()

    def elaborate(self, platform):
        m = Module()

        # -------- Trigger output pin --------
        # "oe" so we can implement an open-drain mode: the FPGA only actively
        # drives the line during the pulse, otherwise the buffer is high-Z and
        # an external pull-up (or pull-down) sets the idle level. In push-pull
        # mode (open_drain=0) the output is always enabled, matching the
        # original behavior.
        m.submodules.trigger_buf = trigger_buf = io.Buffer("io", self.ports.trigger)
        trigger_active = Signal()   # logically active-high; polarity applied at pin
        m.d.comb += [
            trigger_buf.o.eq(trigger_active ^ self.polarity),
            trigger_buf.oe.eq(trigger_active | ~self.open_drain),
        ]

        # -------- UART receiver (stock Glasgow gateware) --------
        # bit_cyc sized to the full range of manual_cyc so the host can reload it
        # at runtime. Initial value doesn't matter; we load it every cycle from
        # the host register.
        m.submodules.uart = uart = UART(
            self.ports,
            bit_cyc=(1 << len(self.manual_cyc)) - 1,
            parity="none",
            stop_bits=1,
        )
        # Hold the UART at its init bit_cyc (effectively paused) until the host
        # configures a real baud rate. Without this guard, `manual_cyc=0` makes
        # `rx_timer` race at every cycle and the UART locks onto garbage during
        # the first few cycles of simulation.
        with m.If(self.manual_cyc >= 2):
            m.d.sync += uart.bit_cyc.eq(self.manual_cyc)

        rx_stb  = Signal()
        rx_byte = Signal(8)
        m.d.comb += [
            rx_stb.eq(uart.rx_rdy),
            rx_byte.eq(uart.rx_data),
            uart.rx_ack.eq(uart.rx_rdy),      # always ack; never back-pressure UART
        ]

        with m.If(uart.rx_ferr | uart.rx_perr | uart.rx_ovf):
            m.d.sync += self.rx_errors.eq(self.rx_errors + 1)

        # -------- Host passthrough --------
        # Every received byte goes out on rx_stream. Best-effort: if the host
        # isn't draining, we drop — the glitch path never waits on this.
        m.d.comb += [
            self.rx_stream.payload.eq(rx_byte),
            self.rx_stream.valid.eq(rx_stb),
            self.rx_flush.eq(rx_stb & rx_byte.matches(0x0A, 0x0D)),
        ]

        # -------- Sliding-window pattern matcher --------
        # On each received byte (while armed), shift into the LSB of `shreg`.
        # After N consecutive bytes b0,b1,...,bN-1 arrive:
        #   shreg slot 0  = bN-1 (most recent)
        #   shreg slot 1  = bN-2
        #   ...
        #   shreg slot N-1 = b0 (oldest)
        # Host packs the configured pattern so that pattern slot j mirrors
        # shreg slot j — i.e. first-received byte at slot (len-1), last-received
        # byte at slot 0. `int.from_bytes(pattern_bytes, 'big')` does this.
        shreg       = Signal(MAX_PATTERN_LEN * 8)
        seen        = Signal(range(MAX_PATTERN_LEN + 1))  # saturating, reset on arm
        match_check = Signal()                            # rx_stb delayed one cycle
        armed_like  = Signal()                            # high in ARMED state only

        # Per-slot comparison. Slots beyond pattern_len are wildcards.
        pattern_matched = Signal()
        m.d.comb += pattern_matched.eq(Cat(*[
            (C(j, len(self.pattern_len)) >= self.pattern_len)
            | (shreg[j*8:(j+1)*8] == self.pattern[j*8:(j+1)*8])
            for j in range(MAX_PATTERN_LEN)
        ]).all())

        # Shift in new bytes only while armed so stale history doesn't survive
        # across arm cycles.
        with m.If(armed_like & rx_stb):
            m.d.sync += shreg.eq(Cat(rx_byte, shreg[:-8]))
            with m.If(seen != MAX_PATTERN_LEN):
                m.d.sync += seen.eq(seen + 1)

        # match_check is rx_stb delayed by one cycle so that pattern_matched is
        # evaluated AFTER the new byte has landed in shreg.
        m.d.sync += match_check.eq(armed_like & rx_stb)

        trigger_fire = Signal()
        m.d.comb += trigger_fire.eq(
            match_check & pattern_matched & (seen >= self.pattern_len)
        )

        # -------- Delay / pulse counters --------
        delay_counter = Signal(32)
        pulse_counter = Signal(32)

        # -------- Glitch FSM --------
        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += self.state.eq(S_IDLE)
                with m.If(self.arm):
                    # Clear transient state so a re-arm starts fresh.
                    m.d.sync += [
                        shreg.eq(0),
                        seen.eq(0),
                    ]
                    m.next = "ARMED"

            with m.State("ARMED"):
                m.d.comb += [
                    self.state.eq(S_ARMED),
                    armed_like.eq(1),
                ]
                with m.If(~self.arm):
                    m.next = "IDLE"
                with m.Elif(trigger_fire):
                    m.d.sync += [
                        self.match_count.eq(self.match_count + 1),
                        delay_counter.eq(self.delay_cyc),
                    ]
                    m.next = "DELAY"

            with m.State("DELAY"):
                m.d.comb += self.state.eq(S_DELAY)
                with m.If(~self.arm):
                    m.next = "IDLE"
                with m.Elif(delay_counter == 0):
                    m.d.sync += pulse_counter.eq(self.pulse_cyc)
                    m.next = "PULSE"
                with m.Else():
                    m.d.sync += delay_counter.eq(delay_counter - 1)

            with m.State("PULSE"):
                m.d.comb += [
                    self.state.eq(S_PULSE),
                    trigger_active.eq(1),
                ]
                with m.If(pulse_counter == 0):
                    m.next = "DONE"
                with m.Else():
                    m.d.sync += pulse_counter.eq(pulse_counter - 1)

            with m.State("DONE"):
                m.d.comb += self.state.eq(S_DONE)
                with m.If(~self.arm):
                    m.next = "IDLE"

        return m
