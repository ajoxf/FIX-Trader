"""Stage 2: the engine must call gateway.propose_entry/propose_exit instead
of executor.place() when the gateway exposes them, must not double-propose
while one is pending, and must leave the FakeGateway-driven path (no
propose_entry) completely alone."""
import datetime as dt

from fixtrader.config import ContractConfig, TraderConfig
from fixtrader.database import Database
from fixtrader.engine import Engine
from fixtrader.fake_gateway import FakeGateway, SimContract


class StubProposalGateway(FakeGateway):
    """Everything FakeGateway already does (book, fills if ever used) PLUS
    propose_entry/propose_exit/pending_algo_proposals, so the engine takes
    the proposal branch instead of executor.place()."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.proposed_entries = []
        self.proposed_exits = []
        self._pending = []

    def propose_entry(self, contract, side, qty, reason='', decision=None):
        token = f'tok-{len(self.proposed_entries)}'
        self.proposed_entries.append(
            {'contract_key': contract.key, 'side': side, 'qty': qty,
             'reason': reason, 'decision': decision})
        self._pending.append({'token': token, 'contract_key': contract.key,
                              'kind': 'entry', 'reason': reason})
        return {'ok': True, 'token': token}

    def propose_exit(self, contract, order_id, reason='', exit_reason=None):
        token = f'tok-exit-{len(self.proposed_exits)}'
        self.proposed_exits.append(
            {'contract_key': contract.key, 'order_id': order_id,
             'reason': reason, 'exit_reason': exit_reason})
        self._pending.append({'token': token, 'contract_key': contract.key,
                              'kind': 'exit', 'reason': reason})
        return {'ok': True, 'token': token}

    def pending_algo_proposals(self):
        return list(self._pending)

    def clear_algo_proposals(self):
        n = len(self._pending)
        self._pending.clear()
        return n

    def _expire_all(self):
        self._pending.clear()


def _contract(key='a'):
    return ContractConfig(key=key, name=key, symbol='AAAV6-AAAX6', venue='SIM',
        tick_size=0.01, tick_value=1.0, contract_multiplier=100.0,
        min_qty=1.0, qty_step=1.0, max_qty=50.0, enabled=True, algo_on=True,
        lookback=30, stats_update_interval_sec=1e9, entry_threshold=2.0,
        quantity=5.0, hurst_enabled=False, edge_filter_enabled=False,
        entry_cooldown_seconds=0.0, entry_order_type='MARKET',
        exit_order_type='MARKET', commission_per_contract=1.0,
        exchange_fee_per_contract=0.0, clearing_fee_per_contract=0.0,
        slippage_budget_ticks=0.0, profit_target_pct=2.0)


def _engine(tmp_path, gateway_cls=StubProposalGateway):
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['a'] = _contract()
    gw = gateway_cls([SimContract('a', mid=0.50, tick_size=0.01,
                                  tick_value=1.0, size=50.0)])
    db = Database(str(tmp_path / 'test.db'))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    return engine, gw


def _warm_and_fire(engine, gw, now):
    for i in range(40):
        px = 0.50 + (0.10 if i % 2 else -0.10)
        gw.set_book('a', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
        engine.poll(now=now)
        now += dt.timedelta(seconds=1)
    rt = engine.runtimes['a']
    fire_px = rt.window.price_at_z(3.0)
    gw.set_book('a', round(fire_px - 0.005, 4), round(fire_px + 0.005, 4), 50, 50)
    engine.poll(now=now)
    return now


def test_engine_proposes_instead_of_sending_when_gateway_supports_it(tmp_path):
    engine, gw = _engine(tmp_path)
    now = gw.now
    _warm_and_fire(engine, gw, now)
    assert len(gw.proposed_entries) == 1
    assert gw.proposed_entries[0]['contract_key'] == 'a'
    # No position exists — a proposal is not a fill.
    assert engine.runtimes['a'].position is None


def test_engine_does_not_double_propose_while_one_is_pending(tmp_path):
    engine, gw = _engine(tmp_path)
    now = gw.now
    now = _warm_and_fire(engine, gw, now)
    assert len(gw.proposed_entries) == 1
    # Poll again with the signal still firing — must NOT propose a second time.
    now += dt.timedelta(seconds=1)
    engine.poll(now=now)
    assert len(gw.proposed_entries) == 1


def test_engine_reproposes_once_the_prior_proposal_expires(tmp_path):
    engine, gw = _engine(tmp_path)
    now = gw.now
    now = _warm_and_fire(engine, gw, now)
    assert len(gw.proposed_entries) == 1
    gw._expire_all()          # simulate the 60s preview window passing
    now += dt.timedelta(seconds=1)
    engine.poll(now=now)
    assert len(gw.proposed_entries) == 2


def test_fake_gateway_path_is_unaffected_no_propose_entry(tmp_path):
    """FakeGateway has no propose_entry — the engine must fall back to the
    ordinary executor.place() path exactly as before this change."""
    engine, gw = _engine(tmp_path, gateway_cls=FakeGateway)
    now = gw.now
    _warm_and_fire(engine, gw, now)
    assert not hasattr(gw, 'propose_entry')
    now2 = now + dt.timedelta(seconds=1)
    engine.poll(now=now2)
    # The ordinary path either has a working order or (once matched) a
    # position — either way, proof the old path still runs untouched.
    rt = engine.runtimes['a']
    assert engine.executor.working_for('a') or (rt.position is not None)
    # And the snapshot must not crash or fabricate proposals for a
    # gateway that doesn't have the concept at all.
    assert engine.snapshot(now=now2)['engine']['algo_proposals'] == []


def test_snapshot_surfaces_pending_algo_proposals(tmp_path):
    engine, gw = _engine(tmp_path)
    now = gw.now
    _warm_and_fire(engine, gw, now)
    snap = engine.snapshot(now=now)
    assert len(snap['engine']['algo_proposals']) == 1
    assert snap['engine']['algo_proposals'][0]['contract_key'] == 'a'


def test_portfolio_risk_gate_blocks_the_propose_path_too(tmp_path):
    """The risk gate must apply identically whether the entry is about to
    be proposed (live TT path) or sent directly (simulator path)."""
    cfg = TraderConfig(path=str(tmp_path / 'config.json'))
    cfg.contracts['a'] = _contract('a')
    cfg.contracts['b'] = _contract('b')
    cfg.settings['RISK_MAX_POSITIONS_TOTAL'] = 1
    gw = StubProposalGateway([
        SimContract('a', mid=0.50, tick_size=0.01, tick_value=1.0, size=50.0),
        SimContract('b', mid=0.50, tick_size=0.01, tick_value=1.0, size=50.0),
    ])
    db = Database(str(tmp_path / 'test.db'))
    engine = Engine(cfg, gw, db=db, simulated=True)
    engine.start()
    now = gw.now
    for i in range(40):
        px = 0.50 + (0.10 if i % 2 else -0.10)
        gw.set_book('a', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
        gw.set_book('b', round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
        engine.poll(now=now)
        now += dt.timedelta(seconds=1)
    for key in ('a', 'b'):
        rt = engine.runtimes[key]
        px = rt.window.price_at_z(3.0)
        gw.set_book(key, round(px - 0.005, 4), round(px + 0.005, 4), 50, 50)
    engine.poll(now=now)

    assert len(gw.proposed_entries) == 1
    blocked_texts = {k: rt.blocked_by for k, rt in engine.runtimes.items()}
    assert any(t and 'portfolio position cap' in t for t in blocked_texts.values())
