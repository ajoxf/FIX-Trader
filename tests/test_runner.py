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


def test_a_second_engine_refuses_to_start_against_a_live_book(tmp_path):
    """Two engines against one book both trade the same signals on the same
    account, and each sees the other's fills as positions it cannot explain.
    An operator double-clicking the launcher is all it takes — and it happened
    here during development, leaving a database written by two processes."""
    import json
    from datetime import datetime, timezone
    a_config(tmp_path)
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'ts': datetime.now(timezone.utc).isoformat(),
        'engine': {'alive': True}, 'contracts': []}))

    with pytest.raises(SystemExit) as e:
        runner.run(config_path=str(tmp_path / 'config.json'),
                   status_path=str(status),
                   command_path=str(tmp_path / 'commands.jsonl'),
                   result_path=str(tmp_path / 'results.json'),
                   simulated=True)
    assert 'Another engine' in str(e.value)
    assert 'cannot explain' in str(e.value)        # it says why, not just no


def test_a_stale_snapshot_is_not_a_running_engine(tmp_path):
    """Refusing to start when nothing is running is the worse failure of the
    two, so the check is deliberately generous."""
    import json
    from datetime import datetime, timedelta, timezone
    status = tmp_path / 'status.json'
    status.write_text(json.dumps({
        'ts': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        'engine': {'alive': True}, 'contracts': []}))
    assert runner.another_engine_is_running(str(status)) is None

    a_config(tmp_path)
    run_once(tmp_path)                             # starts without complaint


def test_no_snapshot_at_all_is_not_a_running_engine(tmp_path):
    assert runner.another_engine_is_running(str(tmp_path / 'nothing.json')) is None


def test_an_edited_config_is_picked_up_while_the_loop_runs(tmp_path):
    """Settings are edited in the WEB process, which writes config.json and
    nothing else. Without the engine reading it back, a saved setting sits on
    disk looking applied while the loop goes on trading the old one."""
    cfg = a_config(tmp_path)
    passes = {'n': 0}

    def stop_after_a_few():
        passes['n'] += 1
        if passes['n'] == 2:
            # Edited between one pass and the next, exactly as the web
            # process would have written it.
            cfg.contracts['fef'].overrides['entry_threshold'] = 3.25
            cfg.save()
        return passes['n'] > 6

    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, should_stop=stop_after_a_few)

    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['contracts'][0]['settings']['entry_threshold'] == 3.25
    assert snap['engine']['config_reloaded_at'] is not None
    assert snap['engine']['config_restart_needed'] == []


def test_an_unreadable_config_does_not_stop_the_loop(tmp_path):
    """A half-written file is not a reason to stop managing live positions."""
    a_config(tmp_path)
    passes = {'n': 0}

    def stop_after_a_few():
        passes['n'] += 1
        if passes['n'] == 2:
            (tmp_path / 'config.json').write_text('{ this is not json')
        return passes['n'] > 6

    runner.run(config_path=str(tmp_path / 'config.json'),
               status_path=str(tmp_path / 'status.json'),
               command_path=str(tmp_path / 'commands.jsonl'),
               result_path=str(tmp_path / 'results.json'),
               simulated=True, should_stop=stop_after_a_few)

    snap = json.loads((tmp_path / 'status.json').read_text())
    assert snap['engine']['alive'] is True
    # the settings it already had are still the ones in force
    assert snap['contracts'][0]['settings']['entry_threshold'] is not None
