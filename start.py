#!/usr/bin/env python3
"""One command. It creates what is missing, starts both processes and opens
the terminal in a window of its own.

    python start.py

Nothing to copy, nothing to edit. On a first run it writes `config.json` and
`.env`, brings the **web UI up first** (the venues are entered on that screen,
so it has to be reachable before there are any), starts the engine, opens the
browser and restarts a crashed child with backoff.

The web process comes up before the engine on purpose: a box with no venue
configured must still reach the screen that configures one.
"""

import argparse
import os
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fixtrader import appwindow                      # noqa: E402
from fixtrader.config import DEFAULT_SETTINGS, TraderConfig  # noqa: E402

#: What a first run gets: three plausible spread contracts against the
#: simulator, so the screen has something on it before a venue exists. They
#: are ordinary config rows and the operator deletes or edits them.
STARTER_CONTRACTS = {
    'fef_v6x6': {
        'name': 'Iron ore Oct/Nov', 'symbol': 'FEFV6-FEFX6', 'venue': 'SIM',
        'tick_size': 0.01, 'tick_value': 1.0, 'contract_multiplier': 100.0,
        'currency': 'USD', 'min_qty': 1.0, 'qty_step': 1.0, 'max_qty': 50.0,
        'decimals': 4, 'enabled': True, 'algo_on': True,
        'lookback': 120, 'stats_update_interval_sec': 10, 'quantity': 5.0,
        'commission_per_contract': 1.20, 'exchange_fee_per_contract': 0.55,
        'clearing_fee_per_contract': 0.15,
    },
    'fef_x6z6': {
        'name': 'Iron ore Nov/Dec', 'symbol': 'FEFX6-FEFZ6', 'venue': 'SIM',
        'tick_size': 0.01, 'tick_value': 1.0, 'contract_multiplier': 100.0,
        'currency': 'USD', 'min_qty': 1.0, 'qty_step': 1.0, 'max_qty': 50.0,
        'decimals': 4, 'enabled': True, 'algo_on': True,
        'lookback': 120, 'stats_update_interval_sec': 10, 'quantity': 4.0,
        'commission_per_contract': 1.20, 'exchange_fee_per_contract': 0.55,
        'clearing_fee_per_contract': 0.15,
    },
    'cl_z6f7': {
        'name': 'WTI Dec/Jan', 'symbol': 'CLZ6-CLF7', 'venue': 'SIM',
        'tick_size': 0.01, 'tick_value': 10.0, 'contract_multiplier': 1000.0,
        'currency': 'USD', 'min_qty': 1.0, 'qty_step': 1.0, 'max_qty': 20.0,
        'decimals': 4, 'enabled': True, 'algo_on': False,
        'lookback': 120, 'stats_update_interval_sec': 10, 'quantity': 2.0,
        'commission_per_contract': 2.50, 'exchange_fee_per_contract': 1.45,
        'clearing_fee_per_contract': 0.20,
    },
}


def first_run(config_path: str, env_path: str) -> None:
    if not os.path.exists(config_path):
        config = TraderConfig(path=config_path)
        config.settings = dict(DEFAULT_SETTINGS)
        from fixtrader.config import ContractConfig
        for key, raw in STARTER_CONTRACTS.items():
            config.contracts[key] = ContractConfig.from_dict(key, raw)
        config.save()
        print(f"[start] wrote {config_path} with {len(config.contracts)} "
              f"example contracts against the simulator")
    if not os.path.exists(env_path):
        with open(env_path, 'w', encoding='utf-8') as fh:
            fh.write("# Secrets live here and NOWHERE else. Never in\n"
                     "# config.json, never in code, never in a log line.\n"
                     "# The Exchanges page writes venue passwords here.\n")
        print(f"[start] wrote {env_path}")


def spawn(argv, name):
    print(f"[start] {name}: {' '.join(argv)}")
    return subprocess.Popen(argv, cwd=HERE)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Start FIX-Trader")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--simulated', action='store_true', default=True)
    args = parser.parse_args(argv)

    first_run(args.config, '.env')
    url = f"http://{args.host}:{args.port}/"
    python = sys.executable

    common = ['--config', args.config, '--status', args.status,
              '--commands', args.commands, '--results', args.results]
    children = {}

    # The WEB first: the venues are entered on that screen, so it has to be
    # reachable before there are any.
    children['web'] = spawn([python, '-m', 'fixtrader.webapp'] + common +
                            ['--host', args.host, '--port', str(args.port)], 'web')
    time.sleep(1.0)
    engine_argv = [python, '-m', 'fixtrader.runner'] + common
    if args.simulated:
        engine_argv.append('--simulated')
    children['engine'] = spawn(engine_argv, 'engine')

    if not args.no_browser:
        appwindow.open_window(url)
    print(f"[start] terminal at {url}")

    backoff = {'web': 1.0, 'engine': 1.0}
    try:
        while True:
            time.sleep(1.0)
            for name, proc in list(children.items()):
                if proc.poll() is None:
                    backoff[name] = 1.0
                    continue
                wait = backoff[name]
                print(f"[start] {name} exited ({proc.returncode}); "
                      f"restarting in {wait:.0f}s")
                time.sleep(wait)
                backoff[name] = min(30.0, wait * 2)
                if name == 'web':
                    children[name] = spawn(
                        [python, '-m', 'fixtrader.webapp'] + common +
                        ['--host', args.host, '--port', str(args.port)], 'web')
                else:
                    children[name] = spawn(engine_argv, 'engine')
    except KeyboardInterrupt:
        print("\n[start] stopping")
    finally:
        for name, proc in children.items():
            if proc.poll() is None:
                proc.terminate()
        for proc in children.values():
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
    return 0


if __name__ == '__main__':
    sys.exit(main())
