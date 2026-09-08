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

    @app.get('/settings')
    def settings_page():
        return render_template('settings.html', asset_version=ASSET_VERSION)

    @app.get('/exchanges')
    def exchanges_page():
        return render_template('exchanges.html', asset_version=ASSET_VERSION)

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

    # -- venues ------------------------------------------------------------

    @app.post('/api/contracts')
    def api_create_contract():
        """Add a contract. The key is derived from the symbol so two rows
        cannot quietly share one."""
        import re
        from .config import ContractConfig
        config = load_config()
        data = dict(request.get_json(silent=True) or {})
        symbol = str(data.get('symbol') or '').strip()
        if not symbol:
            return jsonify({'ok': False, 'error': 'a symbol is required'}), 400
        venue = str(data.get('venue') or '').strip()
        if venue and venue not in config.venues:
            return jsonify({'ok': False,
                            'error': f"no venue {venue!r}"}), 400
        key = data.get('key') or re.sub(r'[^a-z0-9]+', '_',
                                        symbol.lower()).strip('_')
        if key in config.contracts:
            return jsonify({'ok': False,
                            'error': f"{key} already exists"}), 409
        data['symbol'] = symbol
        allowed = {k: v for k, v in data.items() if k != 'key'}
        config.contracts[key] = ContractConfig.from_dict(key, allowed)
        config.save()
        return jsonify({'ok': True, 'key': key})

    @app.delete('/api/contracts/<path:key>')
    def api_delete_contract(key):
        """Refused while anything is open on it — and the refusal says what."""
        config = load_config()
        if key not in config.contracts:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        snap = read_status()
        for c in snap.get('contracts', []):
            if c.get('key') == key and c.get('position'):
                pos = c['position']
                return jsonify({'ok': False, 'error':
                                f"{pos['side']} {pos['qty']:g} is open on "
                                f"this contract. Close it first."}), 409
        config.contracts.pop(key)
        config.save()
        return jsonify({'ok': True})

    @app.get('/api/venues')
    def api_venues():
        """Public form only. The password is reported as set or not set and
        is NEVER returned, masked or otherwise."""
        return jsonify([v.to_public_dict()
                        for v in load_config().venues.values()])

    @app.post('/api/venues/<path:name>')
    def api_save_venue(name):
        """Create or update one venue.

        The password never reaches `config.json`. It goes to `.env` under the
        venue's own key, and only when a non-empty value is sent — an empty
        field means "leave it alone", so re-saving a form that shows no
        password cannot wipe the one that is set.
        """
        from .config import VenueConfig, env_key_for, write_env_value
        config = load_config()
        data = dict(request.get_json(silent=True) or {})
        password = data.pop('password', None)
        data.pop('password_set', None)
        data.pop('name', None)

        existing = config.venues.get(name)
        raw = existing.to_dict() if existing else {}
        raw.update({k: v for k, v in data.items() if k in VENUE_FIELDS})
        raw.setdefault('environment', '')
        raw.setdefault('password_env', env_key_for(name))
        try:
            venue = VenueConfig.from_dict(name, raw)
        except ValueError as e:
            # The environment is the one field with no default: UAT and PROD
            # are separate venues and neither is assumed.
            return jsonify({'ok': False, 'error': str(e)}), 400

        config.venues[name] = venue
        config.save()
        if password:
            write_env_value(venue.password_env, password)
        return jsonify({'ok': True, 'venue': venue.to_public_dict()})

    @app.delete('/api/venues/<path:name>')
    def api_delete_venue(name):
        config = load_config()
        if name not in config.venues:
            return jsonify({'ok': False, 'error': f"no venue {name}"}), 404
        using = [c.key for c in config.contracts.values() if c.venue == name]
        if using:
            # Deleting the venue a contract routes through would leave the
            # contract pointing at nothing, and the refusal names them.
            return jsonify({'ok': False, 'error':
                            f"{len(using)} contract(s) route through it: "
                            f"{', '.join(using)}"}), 409
        config.venues.pop(name)
        config.save()
        return jsonify({'ok': True})

    def _gateway_for(config, venue):
        """A gateway for one venue, for the three buttons. Never the engine's
        — this process renders and asks; it does not trade."""
        contracts = [c for c in config.contracts.values()
                     if c.venue == venue.name]
        if not venue.host:
            from .fake_gateway import FakeGateway, SimContract
            sims = [SimContract(c.key, tick_size=c.tick_size or 0.01,
                                tick_value=c.tick_value or 1.0)
                    for c in contracts]
            return FakeGateway(sims), contracts, True
        from .gateway import FixGateway
        return FixGateway(venue, contracts), contracts, False

    @app.get('/api/venues/<path:name>/<any(connect,test,diagnose):action>')
    def api_venue_action(name, action):
        """Connect, Test and Diagnose.

        Every failure carries the step that fixes it. None of them ever
        returns a credential, and the answer is the venue's own words rather
        than "check the log".
        """
        config = load_config()
        venue = config.venues.get(name)
        if venue is None:
            return jsonify({'ok': False, 'error': f"no venue {name}"}), 404

        gateway, contracts, simulated = _gateway_for(config, venue)
        rows = []
        try:
            gateway.start()
            state = gateway.state()
            ok = state.value == 'LOGGED_ON'
            rows.append({
                'check': 'Session',
                'ok': ok,
                'detail': f"{state.value} — {gateway.state_text()}",
                'fix': '' if ok else (
                    'Check the host, port and comp ids, and that the '
                    'password is set in .env.'),
            })

            if action in ('test', 'diagnose'):
                rows.append({
                    'check': 'Environment',
                    'ok': True,
                    'detail': (f"{venue.environment}"
                               + (' — SIMULATED, no venue is connected'
                                  if simulated else '')),
                    'fix': '',
                })
                rows.append({
                    'check': 'Password',
                    'ok': venue.has_password,
                    # Set or not set. Never the value, and never a masked
                    # version of it either.
                    'detail': ('set in .env as ' + venue.password_env
                               if venue.has_password else
                               'NOT set — the session cannot log on'),
                    'fix': '' if venue.has_password else
                           f"Type it into the password field and save; it is "
                           f"written to .env as {venue.password_env}.",
                })
                rows.append({
                    'check': 'Account',
                    'ok': bool(venue.account),
                    'detail': venue.account or 'not set',
                    'fix': '' if venue.account else
                           'Every order is stamped with it.',
                })

            if action == 'diagnose':
                for contract in contracts:
                    rows.append(_contract_check(gateway, contract, simulated))
                rows.extend(getattr(gateway, 'diagnose', lambda: [])())
        except Exception as e:                              # noqa: BLE001
            rows.append({'check': 'Session', 'ok': False,
                         'detail': f"{type(e).__name__}: {e}", 'fix': ''})
        finally:
            try:
                gateway.stop()
            except Exception:                               # noqa: BLE001
                pass

        return jsonify({
            'ok': all(r['ok'] for r in rows),
            'rows': rows,
            'simulated': simulated,
        })

    @app.post('/api/contracts/<path:key>/read-from-venue')
    def api_read_specs(key):
        """Fill the specifications from the venue's own security definition.

        It REPORTS rather than applies: each field comes back with what the
        venue says and what the config holds, and the operator presses the
        button. A specification changed under a running desk is every money
        figure on that window changing without anybody being told.
        """
        config = load_config()
        contract = config.contracts.get(key)
        if contract is None:
            return jsonify({'ok': False, 'error': f"no contract {key}"}), 404
        venue = config.venues.get(contract.venue)
        if venue is None:
            return jsonify({'ok': False,
                            'error': f"contract {key} has no venue"}), 400

        gateway, _, simulated = _gateway_for(config, venue)
        try:
            gateway.start()
            gateway.subscribe(contract)
            spec = gateway.security_definition(contract)
        finally:
            try:
                gateway.stop()
            except Exception:                               # noqa: BLE001
                pass

        if spec is None:
            return jsonify({'ok': False, 'simulated': simulated,
                            'error': "the venue did not publish a definition "
                                     "for this contract"})
        fields = []
        for field in ('tick_size', 'tick_value', 'contract_multiplier',
                      'currency', 'min_qty', 'qty_step', 'max_qty'):
            theirs = getattr(spec, field, None)
            ours = getattr(contract, field, None)
            fields.append({
                'field': field, 'venue': theirs, 'config': ours,
                'agrees': theirs is None or ours is None or theirs == ours,
                'source': contract.spec_source.get(field, 'unset'),
            })
        return jsonify({'ok': True, 'simulated': simulated, 'fields': fields,
                        'trading_status': spec.trading_status,
                        'note': SIMULATED_SPEC_NOTE if simulated else None})

    return app


#: What may be written to a venue from the UI. `password` is handled on its
#: own and never lands here; anything not on this list is ignored rather than
#: quietly set.
VENUE_FIELDS = (
    'environment', 'broker', 'host', 'port', 'md_host', 'md_port',
    'sender_comp_id', 'target_comp_id', 'sender_sub_id', 'target_sub_id',
    'on_behalf_of_comp_id', 'fix_version', 'username', 'account',
    'heartbeat_sec', 'reset_seq_on_logon', 'use_tls', 'data_dictionary',
    'store_path', 'log_path', 'enabled',
)


#: What the simulator can and cannot tell you. It is built from the contract's
#: own configuration, so it reports back exactly what it was given — which
#: means it can never disagree, and a green Diagnose against it confirms
#: NOTHING about the specifications. Saying so is the difference between a
#: check and the appearance of one.
SIMULATED_SPEC_NOTE = (
    "the simulator reports back what you configured, so this confirms "
    "nothing — these figures are checked against the venue when the FIX "
    "session is wired")


def _contract_check(gateway, contract, simulated=False):
    """One contract against the venue's own definition of it."""
    spec = gateway.security_definition(contract)
    if spec is None:
        return {'check': contract.key, 'ok': False,
                'detail': 'the venue published no definition',
                'fix': 'Check the symbol, and how this venue identifies a '
                       'spread contract (docs/FIX_NOTES.md).'}
    for field in ('tick_size', 'tick_value', 'contract_multiplier'):
        theirs = getattr(spec, field, None)
        ours = getattr(contract, field, None)
        if theirs is not None and ours is not None and theirs != ours:
            return {
                'check': contract.key, 'ok': False,
                'detail': (f"venue says {field} is {theirs}, "
                           f"config says {ours}"),
                # Not a warning. Every money figure on that window runs
                # through these two numbers.
                'fix': f"Read the specifications from the venue and accept "
                       f"{theirs}, or correct the config.",
            }
    detail = (f"tick {spec.tick_size} value {spec.tick_value} "
              f"mult {spec.contract_multiplier} {spec.currency} "
              f"{spec.trading_status or ''}").strip()
    if simulated:
        detail += f" — {SIMULATED_SPEC_NOTE}"
    return {'check': contract.key, 'ok': True, 'detail': detail, 'fix': ''}


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
