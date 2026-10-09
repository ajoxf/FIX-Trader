"""TT's FIX prices are not the prices a trader knows.

TT sends Crude as 9057 and Gold as 41998: FIX price x DisplayFactor (9787)
is the price on every other screen (90.57, 4199.8). The exchange's tick
(16552) is already in trader prices, so a FIX price read raw made every
money figure wrong by the factor. The conversion is made once, at the FIX
boundary, in both directions."""
from types import SimpleNamespace

import pytest

from fixtrader.gateway import AlgoOrderRouter
from fixtrader.manual_terminal import ManualTerminal


class Session:
    def __init__(self):
        self.state = SimpleNamespace(status='CONNECTED')
        self.sent = []

    def is_running(self):
        return True

    def send(self, msg, fields):
        self.sent.append((msg, fields))


def define(terminal, request_id, key, **tags):
    fields = {'35': 'd', '320': request_id, '48': key, '55': 'CL', '207': 'CME',
              '167': 'FUT', '200': '202612', '107': 'Crude Dec26', '15': 'USD'}
    fields.update(tags)
    terminal.on_message('Market Data', fields,
                        '\x01'.join(k + '=' + v for k, v in fields.items()) + '\x01')


@pytest.fixture
def terminal(tmp_path):
    gateway = SimpleNamespace(_sessions={'Market Data': Session(), 'Order Routing': Session()},
                              venue=SimpleNamespace(account='ACC'), _redact=lambda v: v)
    t = ManualTerminal(gateway, str(tmp_path / 'manual.db'))
    t.poll()
    rid = t.lookup({'exchange': 'CME', 'symbol': 'CL', 'security_type': 'FUT'})['request_id']
    # Crude: TT's tick 1 in FIX units, the exchange's 0.01, DisplayFactor 0.01.
    define(t, rid, 'CL1', **{'969': '1', '16552': '0.01', '16554': '1000', '9787': '0.01'})
    # Gold, without a 9787: the factor is the ratio of the two ticks.
    define(t, rid, 'GC1', **{'969': '1', '16552': '0.1', '16554': '100'})
    # A product TT quotes in trader prices already: no factor, none needed.
    define(t, rid, 'ES1', **{'969': '0.25', '16552': '0.25', '16554': '50'})
    # E-mini as TT UAT defines it: tick 25 in FIX units, 16552 ALSO 25.
    define(t, rid, 'ESZ6', **{'969': '25', '16552': '25', '9787': '0.01'})
    for key in ('CL1', 'GC1', 'ES1', 'ESZ6'):
        t.add({'security_id': key})
    return t


def test_the_factor_is_read_from_9787_or_the_ticks(terminal):
    assert str(terminal.price_factor('CL1')) == '0.01'
    assert terminal.watch['CL1']['display_factor_source'] == '9787'
    assert str(terminal.price_factor('GC1')) == '0.1'
    assert terminal.watch['GC1']['display_factor_source'] == '16552/969'
    assert str(terminal.price_factor('ES1')) == '1'
    assert terminal.price_factor('nothing') is None          # unknown, not 1


def test_both_directions_are_exact(terminal):
    assert terminal.to_display('CL1', '9057') == 90.57
    assert terminal.to_display('GC1', '41998') == 4199.8
    assert terminal.to_fix('CL1', 90.57) == '9057'
    assert terminal.to_fix('GC1', '4199.8') == '41998'
    assert terminal.to_fix('ES1', '7837.25') == '7837.25'
    assert terminal.to_fix('CL1', '-0.5') == '-50'            # spreads go negative


def test_the_book_is_in_trader_prices(terminal):
    request = terminal.subscriptions['CL1']
    raw = (f'35=W\x01262={request}\x01268=2\x01269=0\x01270=9056\x01271=3\x01'
           f'269=1\x01270=9057\x01271=4\x01')
    terminal.on_message('Market Data', {'35': 'W'}, raw)
    book = terminal.books['CL1']
    assert book['bid'] == 90.56 and book['ask'] == 90.57


def test_a_ticket_goes_out_in_fix_units_and_fills_come_back_in_trader_prices(terminal):
    preview = terminal.preview({'security_id': 'CL1', 'side': 'BUY', 'order_type': 'LIMIT',
                                'quantity': '1', 'price': '90.55', 'tif': 'DAY',
                                'account': 'ACC', 'open_close': 'O'})
    out = terminal.submit({'token': preview['token'], 'confirmed': True})
    sent = [dict(f) for k, f in terminal.gateway._sessions['Order Routing'].sent if k == 'D']
    assert sent[-1]['44'] == '9055'                           # what TT expects
    order = terminal.orders[out['order_id']]
    assert order['units'] == 'display'
    terminal.on_message('Order Routing', {'35': '8', '11': order['current_id'], '37': 'TT1',
                                          '150': '2', '39': '2', '17': 'E1', '32': '1',
                                          '31': '9055', '6': '9055', '14': '1', '151': '0'}, '')
    assert order['avg_price'] == 90.55
    fill = terminal.snapshot()['fills'][0]
    assert fill['price'] == 90.55


def test_a_ticket_off_the_trader_tick_is_refused(terminal):
    with pytest.raises(ValueError, match='tick'):
        terminal.preview({'security_id': 'CL1', 'side': 'BUY', 'order_type': 'LIMIT',
                          'quantity': '1', 'price': '90.555', 'tif': 'DAY', 'account': 'ACC'})


def test_orders_recorded_in_fix_units_are_brought_over_once(terminal):
    order = {'id': 'FTM-old', 'current_id': 'FTM-old', 'ids': ['FTM-old'], 'status': 'FILLED',
             'ticket': {'security_id': 'CL1', 'side': 'BUY', 'price': '9055',
                        'stop_price': None, 'order_type': 'LIMIT', 'quantity': '1',
                        'instrument': terminal.watch['CL1']},
             'avg_price': 9055.0, 'filled_qty': 1, 'decision': {'price': 9056.0}}
    terminal.orders['FTM-old'] = order
    terminal._units_known = frozenset()
    terminal._migrate_units()
    assert order['avg_price'] == pytest.approx(90.55)
    assert order['ticket']['price'] == '90.55'
    assert order['decision']['price'] == pytest.approx(90.56)
    terminal._units_known = frozenset()
    terminal._migrate_units()                                 # once, not twice
    assert order['avg_price'] == pytest.approx(90.55)


def test_the_algo_router_converts_both_ways(terminal):
    gw = SimpleNamespace(venue=SimpleNamespace(name='TT', account='ACC'),
                         contracts=[SimpleNamespace(key='cl', security_id='CL1')],
                         _redact=lambda t: t, terminal=terminal)
    r = AlgoOrderRouter(gw)
    assert r._to_fix('CL1', 90.57) == '9057'
    r.on_message('8', {'35': '8', '150': 'F', '39': '2', '17': 'E9', '11': 'X-1',
                       '48': 'CL1', '54': '1', '32': '1', '31': '9057'}, '')
    assert r.take_tape()[0]['price'] == 90.57


def test_a_router_without_a_terminal_leaves_prices_alone():
    """The control: no TT definitions, no conversion."""
    gw = SimpleNamespace(venue=SimpleNamespace(name='TT', account='ACC'),
                         contracts=[], _redact=lambda t: t)
    r = AlgoOrderRouter(gw)
    assert r._to_fix('X', 90.57) == '90.57'
    assert r._to_display('X', '9057') == 9057.0


# -- the recording the band is rebuilt from ------------------------------------

def test_a_recording_in_fix_units_is_rescaled_once_and_entries_wait_without_a_factor(tmp_path):
    from datetime import timedelta
    from decimal import Decimal
    from tests.test_engine_signal import build

    engine, gw, db = build(tmp_path)
    rt = engine.runtimes['fef']
    rt.contract.security_id = 'CL1'
    now = gw.now
    # Mids recorded by an earlier version, in TT's FIX units.
    db.save_samples('fef', [(now - timedelta(seconds=30 - i), 9050.0 + i)
                            for i in range(10)])
    factor = {'value': None}
    gw.terminal = SimpleNamespace(price_factor=lambda sid: factor['value'],
                                  open_business=lambda sid: [])

    engine.poll(now=now)
    halted = engine.halted_by(rt, now)
    assert 'display factor' in halted                         # entries wait
    assert db.price_unit('fef') is None
    assert len(db.samples_between('fef')) == 10               # nothing recorded

    factor['value'] = Decimal('0.01')
    engine.poll(now=now)
    prices = [p for _, p in db.samples_between('fef')]
    assert prices[0] == pytest.approx(90.50)                  # rescaled
    assert all(p < 100 for p in prices)                       # one unit throughout
    assert db.price_unit('fef') == 0.01
    assert rt.units_note is None
    engine.poll(now=now)
    engine.poll(now=now)
    assert [p for _, p in db.samples_between('fef')][0] == pytest.approx(90.50)  # once


def test_a_recording_already_in_trader_units_is_left_alone(tmp_path):
    """The control: the factor recorded beside the samples is the one TT
    gives, so nothing is rescaled."""
    from datetime import timedelta
    from decimal import Decimal
    from tests.test_engine_signal import build

    engine, gw, db = build(tmp_path)
    engine.runtimes['fef'].contract.security_id = 'CL1'
    db.rescale_prices('fef', 1.0, 0.01)
    db.save_samples('fef', [(gw.now - timedelta(seconds=5), 90.5)])
    gw.terminal = SimpleNamespace(price_factor=lambda sid: Decimal('0.01'),
                                  open_business=lambda sid: [])
    engine.poll(now=gw.now)
    assert db.samples_between('fef')[0][1] == 90.5


def test_the_tick_is_tts_tick_times_the_factor_even_when_16552_is_in_fix_units(terminal):
    """ES on TT UAT: 969=25, 16552=25, 9787=0.01. The tick a trader prices in
    is 0.25 — read as 25, every price but whole hundreds was refused."""
    assert terminal.watch['ESZ6']['tick_size'] == '0.25'
    preview = terminal.preview({'security_id': 'ESZ6', 'side': 'BUY', 'order_type': 'LIMIT',
                                'quantity': '1', 'price': '7919.25', 'tif': 'DAY',
                                'account': 'ACC', 'open_close': 'O'})
    assert dict(preview['fields'])['44'] == '791925'
    with pytest.raises(ValueError, match='tick'):           # the control: off the tick
        terminal.preview({'security_id': 'ESZ6', 'side': 'BUY', 'order_type': 'LIMIT',
                          'quantity': '1', 'price': '7919.30', 'tif': 'DAY',
                          'account': 'ACC', 'open_close': 'O'})
