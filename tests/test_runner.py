"""The engine loop must outlive anything that goes wrong around it."""
import json

import pytest

from fixtrader import atomicfile, runner
from fixtrader.config import ContractConfig, TraderConfig


def a_config(tmp_path):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.settings['DATABASE_PATH'] = str(tmp_path / 'f.db')
    cfg.contracts['fef'] = ContractConfig(
        key='fef', name='Iron ore Oct/Nov', symbol='FEFV6-FEFX6', venue='SIM',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        lookback=20)
    cfg.save()
    return cfg


def run_once(tmp_path, **kw):
    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, once=True, **kw)


def test_a_pass_publishes_a_snapshot(tmp_path):
    a_config(tmp_path)
    run_once(tmp_path)
    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['contracts'][0]['key'] == 'fef'
    assert snap['engine']['alive'] is True


def test_the_engine_survives_a_snapshot_it_cannot_publish(monkeypatch, tmp_path):
    """The failure this test exists for actually happened, on Windows, on the
    first run: `os.replace` cannot rename over a file another process has
    open, the PermissionError came out of the loop, and the engine STOPPED —
    while positions were live. The snapshot is what the screen reads; the loop
    is what manages the money. A display file must never be able to stop it."""
    a_config(tmp_path)

    def refuses(*a, **k):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(atomicfile, 'write_json', refuses)
    run_once(tmp_path)                     # must return, not raise
    assert not (tmp_path / 'status.json').exists()


def test_a_restart_never_replays_yesterdays_commands(tmp_path):
    """A KILL ALL from last night must not fire again at breakfast."""
    from fixtrader.commands import CommandBridge
    a_config(tmp_path)
    bridge = CommandBridge(str(tmp_path / 'commands.jsonl'),
                           str(tmp_path / 'results.json'))
    old = bridge.submit('kill_all', '')
    run_once(tmp_path)                     # primes past it without running it
    assert bridge.result(old) is None
