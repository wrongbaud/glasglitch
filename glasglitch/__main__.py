"""Direct CLI entry point: `uv run python -m glasglitch ...`.

Thin wrapper that wires GlitchApplet into an argparse parser, finds a Glasgow
device, and drives build → setup → run.
"""

import argparse
import asyncio
import logging
import sys

from glasgow.applet import GlasgowAppletArguments, GlasgowAppletError
from glasgow.hardware.assembly import HardwareAssembly

from .applet import GlitchApplet


def main():
    parser = argparse.ArgumentParser(
        prog="glasglitch", description=GlitchApplet.description)
    parser.add_argument("-v", "--verbose", action="count", default=0,
        help="increase verbosity (-v info, -vv debug, -vvv trace)")
    parser.add_argument("--serial", metavar="SN", type=str, default=None,
        help="Glasgow device serial number (default: auto-detect)")

    access = GlasgowAppletArguments("glasglitch")
    GlitchApplet.add_build_arguments(parser, access)
    GlitchApplet.add_setup_arguments(parser)
    GlitchApplet.add_run_arguments(parser)

    args = parser.parse_args()

    level = [logging.WARNING, logging.INFO, logging.DEBUG, 5][min(args.verbose, 3)]
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    try:
        asyncio.run(_run(args))
    except GlasgowAppletError as e:
        logging.getLogger("glasglitch").error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


async def _run(args):
    assembly = await HardwareAssembly.find_device(serial=args.serial)
    applet = GlitchApplet(assembly)
    applet.build(args)
    async with assembly:
        await applet.setup(args)
        await applet.run(args)


if __name__ == "__main__":
    main()
