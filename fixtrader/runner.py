"""The engine process: poll, drain commands, publish the snapshot.

It writes `status.json` atomically on every pass. The web process reads that
file and nothing else, so a slow browser cannot slow the loop and a dead
engine shows up as a snapshot that has stopped moving — which the banner says
out loud rather than leaving as a quiet market.
"""

import argparse
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from . import atomicfile
from .commands import CommandBridge, apply_command
from .config import TraderConfig
from .database import Database
from .engine import Engine

logger = logging.getLogger("fixtrader.runner")


def build_gateway(config, simulated: bool):
    """The simulator, or the real session once it is wired.

    Until a venue is configured this runs against `FakeGateway` and the
    screen says SIMULATED — which is a true statement about what is on it,
    and a good deal safer than a screen that looks live and is not.
    """
    if simulated or not config.venues:
        from .fake_gateway import FakeGateway, SimContract
        sims = []
        for c in config.contracts.values():
            sims.append(SimContract(
                c.key, mid=0.5, tick_size=c.tick_size or 0.01,
                tick_value=c.tick_value or 1.0,
                multiplier=c.contract_multiplier or 100.0,
                currency=c.currency or "USD"))
        return FakeGateway(sims), True
    from .gateway import FixGateway
    venue = next(iter(config.venues.values()))
    return FixGateway(venue, list(config.contracts.values())), False


def run(config_path: str = "config.json", status_path: str = "status.json",
        command_path: str = "commands.jsonl",
        result_path: str = "results.json", simulated: bool = False,
        once: bool = False) -> None:
    config = TraderConfig.from_file(config_path)
    db = Database(config.settings.get('DATABASE_PATH', 'fixtrader.db'))
    gateway, is_sim = build_gateway(config, simulated)
    engine = Engine(config, gateway, db=db, simulated=is_sim)
    bridge = CommandBridge(command_path, result_path)
    bridge.prime()                    # a restart never replays a KILL ALL
    engine.start()

    stopping = {'now': False}

    def _stop(signum, frame):
        stopping['now'] = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass                      # not the main thread; the caller stops us

    interval = float(config.settings.get('ENGINE_POLL_SEC', 0.1) or 0.1)
    logger.info("engine up — %d contracts, %s", len(engine.runtimes),
                "simulated" if is_sim else "live gateway")
    try:
        while not stopping['now']:
            started = time.monotonic()
            if is_sim:
                gateway.advance(seconds=interval)
            for command in bridge.drain():
                bridge.record(command['id'], apply_command(engine, command))
            engine.poll()
            atomicfile.write_json(status_path, engine.snapshot())
            if once:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, interval - elapsed))
    finally:
        # Sweep our own working orders at shutdown, scoped to our own ids.
        engine.stop()
        logger.info("engine down")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="FIX-Trader engine")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--simulated', action='store_true',
                        help="run against the simulator even if a venue exists")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [engine] %(message)s")
    run(args.config, args.status, args.commands, args.results, args.simulated)
    return 0


if __name__ == '__main__':
    sys.exit(main())
