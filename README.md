# glasglitch

UART-triggered glitch controller built on the [Glasgow Interface Explorer](https://glasgow-embedded.org/).

Watch a target's UART output, wait for a specific byte sequence, then fire a
timing-deterministic pulse on a dedicated trigger pin that can drive a voltage
crowbar MOSFET, an EMFI coil driver, or any other glitch hardware.

The critical latency path — UART RX → pattern match → trigger pin — lives
entirely in FPGA gateware, so jitter is bounded to a handful of system clock
cycles (Glasgow C3 runs at 48 MHz ≈ 21 ns per cycle).

## Architecture

Three layers, following the standard Glasgow applet v2 split:

```
glasglitch/
├── gateware.py    # Amaranth HDL — runs on the FPGA
├── interface.py   # Host-side Python wrapper around the gateware
└── applet.py      # Glasgow CLI integration
```

- **Gateware** (`GlitchComponent`): UART receiver + sliding-window pattern
  matcher + one-shot glitch FSM driving the trigger pin. All timing-critical
  logic.
- **Interface** (`GlitchInterface`): host-side class that registers the
  component with a Glasgow `Assembly`, wires its ports to host-visible
  registers and FIFOs, and exposes high-level async methods (`arm`,
  `wait_done`, `set_pattern`, …).
- **Applet** (`GlitchApplet`): `GlasgowAppletV2` subclass with a CLI for
  configuring pins, baud, pattern, delay, and pulse width, and running
  campaigns.

## Gateware design

### Glitch FSM

```
         arm=1         match         delay_cyc elapsed       pulse_cyc elapsed
IDLE ───────────▶ ARMED ─────▶ DELAY ───────────────▶ PULSE ──────────────────▶ DONE
  ▲               │                                                              │
  └──── arm=0 ◀───┴────── arm=0 from any state aborts and returns to IDLE ───────┘
```

The trigger pin is driven only in the `PULSE` state; `polarity` XORs the pin
at the pad so active-low drivers (common for crowbar boards) are a one-bit
config flag, not a separate build.

### Pattern matcher

A single `MAX_PATTERN_LEN * 8`-bit shift register captures the last N UART
bytes. On each received byte (while armed), the register shifts with the new
byte at the LSB slot. The match is a big AND of per-slot comparisons:

```
match_j = (j >= pattern_len) | (shreg[j] == pattern[j])    for j in 0..N-1
matched = AND of all match_j
```

Slots at or beyond `pattern_len` wildcard to true, so only the first
`pattern_len` bytes (the low-address slots) actually matter.

A `seen` counter gates the match on "received at least pattern_len bytes since
arm" so startup state never produces a spurious match — important if your
pattern happens to contain 0x00.

The match check is pipelined one cycle after the UART `rx_rdy` strobe so that
the comparator evaluates against the updated shift register.

### Pattern packing convention

The pattern bytes the host provides (in UART arrival order) are packed so that
**slot 0 of the shift register is the most recently received byte**. This
means the first-received pattern byte goes into slot `pattern_len - 1`, and
the last-received byte goes into slot 0. The host encodes this with a single
big-endian conversion:

```python
pattern_reg = int.from_bytes(pattern_bytes, "big")
```

## Register map

All config registers are `In` (host writes, gateware reads); all status
registers are `Out` (gateware writes, host reads).

| Name          | Dir | Width | Meaning                                             |
|---------------|-----|-------|-----------------------------------------------------|
| `arm`         | In  | 1     | 1 = FSM runs; 0 = force IDLE                        |
| `polarity`    | In  | 1     | 0 = active-high pulse, 1 = active-low               |
| `pattern`     | In  | 128   | Packed pattern (see above)                          |
| `pattern_len` | In  | 5     | Number of bytes (1..16) actually compared           |
| `manual_cyc`  | In  | 20    | UART bit period in system clocks                    |
| `delay_cyc`   | In  | 32    | Clocks between match and pulse rise                 |
| `pulse_cyc`   | In  | 32    | Pulse width in clocks                               |
| `state`       | Out | 3     | 0=IDLE 1=ARMED 2=DELAY 3=PULSE 4=DONE               |
| `match_count` | Out | 16    | Increments on every successful trigger              |
| `rx_errors`   | Out | 16    | UART framing/parity/overflow count                  |

Plus an `Out(stream.Signature(8))` pipe (`rx_stream`) that carries every
received byte back to the host for context-logging around glitch shots.

## Usage

Dependencies are managed by [`uv`](https://docs.astral.sh/uv/) — no system
yosys/nextpnr required. The `builtin-toolchain` extra pulls in WASM-packaged
synthesis via `yowasp-yosys` and `yowasp-nextpnr-ice40`. First bitstream build
takes a minute or two; subsequent runs use the cached bitstream.

```bash
uv sync                         # one-time; installs glasgow + toolchain
uv run glasgow list             # confirm the device enumerates
```

```bash
# Fire once — wait for the literal string "Starting kernel" on the target's
# TX, then pulse the trigger pin 10 µs later for 200 ns.
uv run python -m glasglitch -v \
    --rx A0 --trigger A1 -V 3.3 -b 115200 \
    fire -s "Starting kernel" --delay-us 10 --pulse-ns 200 --log-rx
```

Pattern can be supplied as a string (with `\n`, `\r`, `\xNN` escapes) or as
raw hex (`-p 4f4b` → `b"OK"`). Delay and pulse width each accept `cyc`, `ns`,
`us`, or `ms` units.

```bash
# Campaign: drive a crowbar MOSFET (active-low) at -200 ns relative to the
# target's "unlock:" prompt. Here we set pulse via explicit cycles for precise
# timing on Glasgow C3 (48 MHz → 21 ns/cycle).
uv run python -m glasglitch \
    --rx A2 --trigger A3 -V 1.8 -b 1500000 --active-low \
    fire -p 756e6c6f636b3a --delay-cyc 9 --pulse-cyc 2
```

Passive monitoring (no triggering):

```bash
uv run python -m glasglitch --rx A0 --trigger A1 -b 115200 monitor
```

### Simulation tests

```bash
uv run python -m unittest glasglitch.test -v
```

## Status

- [x] Gateware (`GlitchComponent`)
- [x] Host interface (`GlitchInterface`)
- [x] CLI applet (`GlitchApplet`) with `fire` / `monitor` subcommands
- [x] Simulation tests (5 cases: match/no-match, pin pulse, polarity, disarm)
- [x] On-device bring-up on Glasgow C3 — bitstream builds, FPGA programs, FSM
      arms and times out cleanly with `rx_errors=0`
- [ ] Hardware loopback verification (scope the trigger pin against a live
      UART source to confirm delay/width timing)

### Known gotchas

- `manual_cyc` must be non-zero for the stock Glasgow UART to behave. The
  gateware guards against writing `bit_cyc=0`; the host-side `set_baud`
  additionally rejects rates outside the 20-bit register range.
- `arm` is a level, not a strobe: set 0→1 to arm, 1→0 to disarm/abort. After
  a fire (`state == DONE`), the host must clear `arm` before re-arming.
- The RX passthrough stream is best-effort — if the host doesn't drain it,
  bytes are dropped rather than back-pressuring the UART. The glitch trigger
  path never depends on the passthrough.
