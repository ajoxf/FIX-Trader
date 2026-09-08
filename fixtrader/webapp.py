"""The Flask process: it renders and it asks. **It never trades.**

Every route here either reads `status.json` or drops a command on the bridge
for the engine to act on. Nothing in this file touches a venue, and nothing in
it can send an order — that separation is what stops a browser crash from
stopping an exit.

The other rule this file keeps: **no secret ever leaves it.** A venue's
password is reported as set or not set; it is never returned, not even masked,
because a masked value is one that gets echoed back into a form and saved over
the real one.
"""

import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from flask import Flask, jsonify, render_template, request

from . import atomicfile
from .commands import CommandBridge
from .config import TraderConfig

#: How stale the snapshot may be before the screen calls the engine dead. It
#: is a multiple of the refresh rather than a fixed number of seconds: a desk
#: running at 2s must not be told its engine has died every other pass.
STALE_SNAPSHOT_MULTIPLE = 8.0
ASSET_VERSION = str(int(time.time()))


def create_app(config_path: str = "config.json",
               status_path: str = "status.json",
               command_path: str = "commands.jsonl",
               result_path: str = "results.json") -> Flask:
    app = Flask(__name__)
    bridge = CommandBridge(command_path, result_path)

    def load_config() -> TraderConfig:
        """Read from disk on every request. The engine also writes this file,
        and a cached copy would show the operator a setting they changed
        minutes ago as though it had not taken."""
        return TraderConfig.from_file(config_path)

    def read_status() -> Dict[str, Any]:
        snap = atomicfile.read_json(status_path, default=None)
        if not snap:
            return {
                'ts': None,
                'engine': {'alive': False, 'text': (
                    "the engine has not published a snapshot yet — it may "
                    "still be starting")},
                'contracts': [],
            }
        # Is it moving? A snapshot that has stopped is a dead engine, and it
        # must never be mistaken for a quiet market.
        try:
            ts = datetime.fromisoformat(snap['ts'])
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = None
        refresh = float((snap.get('engine') or {}).get('refresh_sec', 0.5) or 0.5)
        snap.setdefault('engine', {})['snapshot_age_sec'] = (
            round(age, 1) if age is not None else None)
        if age is not None and age > refresh * STALE_SNAPSHOT_MULTIPLE:
            snap['engine']['alive'] = False
            snap['engine']['text'] = (
                f"the engine last published {age:.0f}s ago — these prices are "
                f"not live")
        return snap

    # -- pages -------------------------------------------------------------

    @app.get('/')
    def index():
        return render_template('index.html', asset_version=ASSET_VERSION)

    # -- the screen --------------------------------------------------------

    @app.get('/api/snapshot')
    def api_snapshot():
        return jsonify(read_status())

    @app.post('/api/command')
    def api_command():
        data = request.get_json(silent=True) or {}
        action = data.get('action')
        if not action:
            return jsonify({'ok': False, 'error': 'no action'}), 400
        command_id = bridge.submit(action, data.get('contract', ''),
                                   data.get('args') or {})
        return jsonify({'ok': True, 'id': command_id})

    @app.get('/api/result/<command_id>')
    def api_result(command_id):
        result = bridge.result(command_id)
        if result is None:
            return jsonify({'ok': None, 'pending': True})
        return jsonify(result)

    # -- configuration -----------------------------------------------------

    @app.get('/api/settings')
    def api_settings():
        return jsonify(load_config().settings)

    @app.post('/api/settings')
    def api_save_settings():
        config = load_config()
        data = request.get_json(silent=True) or {}
        restart = config.structural_changes(data)
        config.settings.update(data)
        config.save()
        return jsonify({'ok': True, 'restart_needed': restart})

    @app.get('/api/contracts')
    def api_contracts():
        config = load_config()
        return jsonify([
            dict(c.to_dict(), key=c.key,
                 effective=c.settings_with_defaults(config.settings))
            for c in config.contracts.values()])

    @app.post('/api/contracts/<path:key>')
    def api_save_contract(key):
        config = load_config()
        data = request.get_json(silent=True) or {}
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        from .config import CONTRACT_DEFAULTS
        for field in ('name', 'symbol', 'venue', 'decimals', 'enabled',
                      'session_open', 'session_close'):
            if field in data:
                setattr(contract, field, data[field])
        for field in ('tick_size', 'tick_value', 'contract_multiplier',
                      'min_qty', 'qty_step', 'max_qty'):
            if field in data and data[field] not in (None, ''):
                setattr(contract, field, float(data[field]))
                # A number the operator typed is an OVERRIDE, and the window
                # says so — it is not the venue's answer any more.
                contract.spec_source[field] = 'operator'
        for field in CONTRACT_DEFAULTS:
            if field in data:
                value = data[field]
                # Blank clears the override; 0 is a real number and sets one.
                contract.overrides[field] = None if value in (None, '') else value
        config.save()
        return jsonify({'ok': True,
                        'effective': contract.settings_with_defaults(config.settings)})

    @app.get('/api/venues')
    def api_venues():
        """Public form only. The password is reported as set or not set and
        is NEVER returned, masked or otherwise."""
        return jsonify([v.to_public_dict()
                        for v in load_config().venues.values()])

    return app


def main(argv=None) -> int:
    import argparse
    import logging
    parser = argparse.ArgumentParser(description="FIX-Trader web")
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    parser.add_argument('--commands', default='commands.jsonl')
    parser.add_argument('--results', default='results.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [web] %(message)s")
    app = create_app(args.config, args.status, args.commands, args.results)
    app.run(host=args.host, port=args.port, threaded=True, use_reloader=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
