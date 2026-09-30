from types import SimpleNamespace

from fixtrader.gateway import FixGateway
from fixtrader.config import VenueConfig


class MarketSession:
    def __init__(self):
        self.state = SimpleNamespace(status='CONNECTED')
        self.sent = []

    def is_running(self):
        return True

    def send(self, message_type, fields):
        self.sent.append((message_type, fields))


def venue():
    return VenueConfig(name='TT-UAT', environment='UAT', host='or.example',
                       port=11502, fix_version='FIX.4.2',
                       sender_comp_id='ORDER', target_comp_id='TT',
                       password_env='TEST_OR', md_host='md.example',
                       md_port=11503, md_sender_comp_id='MARKET',
                       md_password_env='TEST_MD')


def test_configured_contract_is_subscribed_with_tt_identity_and_book_is_live(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    session = MarketSession()
    gateway._sessions['Market Data'] = session
    contract = SimpleNamespace(key='spread-key', name='Spread',
                               symbol='LEG1-LEG2', security_id='98765',
                               security_exchange='CME')

    gateway.subscribe(contract)

    assert len(session.sent) == 1
    message_type, fields = session.sent[0]
    assert message_type == 'V'
    field_map = dict(fields)
    assert field_map['55'] == 'LEG1-LEG2'
    assert field_map['48'] == '98765'
    assert field_map['207'] == 'CME'

    raw = ('35=W\x01262=%s\x01268=2\x01269=0\x01270=99.5\x01271=4'
           '\x01269=1\x01270=100.5\x01271=6\x01') % field_map['262']
    gateway.terminal.on_message('Market Data', {'35': 'W'}, raw)
    book = gateway.top_of_book('spread-key')
    assert book is not None
    assert (book.bid, book.ask, book.mid) == (99.5, 100.5, 100.0)


def test_live_bridge_does_not_fabricate_a_quote_for_unseen_contract(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    assert gateway.top_of_book('never-seen') is None


def test_all_contract_ids_keep_their_tt_definition_and_quotes_separate(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    session = MarketSession()
    gateway._sessions['Market Data'] = session
    for key, security_id in [('front', '101'), ('next', '102'), ('spread', '103')]:
        gateway.terminal.catalogue[security_id] = {
            'security_id': security_id, 'symbol': 'ES', 'exchange': 'CME',
            'maturity': security_id, 'security_type': 'FUT',
            'parameters': {'16552': '0.25'}, 'full_depth': False}
        gateway.subscribe(SimpleNamespace(key=key, symbol='ES', name=key,
                          security_id=security_id, security_exchange='CME', tick_size=1))
        assert gateway.terminal.watch[security_id]['maturity'] == security_id
    for index, (key, security_id) in enumerate([('front', '101'), ('next', '102'), ('spread', '103')]):
        request = gateway.terminal.subscriptions[security_id]
        raw = (f'35=W\x0134={index + 1}\x01262={request}\x01268=2\x01'
               f'269=0\x01270={100 + index}\x01269=1\x01270={101 + index}\x01')
        gateway.terminal.on_message('Market Data', {'35': 'W', '34': str(index + 1)}, raw)
    assert [gateway.top_of_book(key).mid for key in ('front', 'next', 'spread')] == [100.5, 101.5, 102.5]


def test_one_sided_book_and_unchanged_bid_ask_timestamp(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    session = MarketSession()
    gateway._sessions['Market Data'] = session
    gateway.subscribe(SimpleNamespace(key='x', name='X', symbol='X',
                      security_id='101', security_exchange='CME'))
    request = gateway.terminal.subscriptions['101']
    gateway.terminal.on_message('Market Data', {'35': 'W'},
        f'35=W\x01262={request}\x01268=1\x01269=1\x01270=101\x01')
    assert gateway.top_of_book('x').ask == 101
    assert gateway.top_of_book('x').mid is None
    assert gateway.terminal.books['101']['timestamp']
    stamp = gateway.terminal.books['101']['timestamp']
    gateway.terminal.on_message('Market Data', {'35': 'X'},
        f'35=X\x01262={request}\x01268=1\x01269=2\x01270=100\x01')
    assert gateway.terminal.books['101']['timestamp'] == stamp
    assert gateway.terminal.books['101']['book_updated_at']
    assert gateway.drain_market_data()[-1].received_at.isoformat() == gateway.terminal.books['101']['book_updated_at']


def test_reconnect_resubscribes_all_ids_and_rejects_old_books(tmp_path):
    gateway = FixGateway(venue(), manual_path=str(tmp_path / 'manual.db'))
    first = MarketSession()
    gateway._sessions['Market Data'] = first
    for security_id in ('101', '102'):
        gateway.subscribe(SimpleNamespace(key=security_id, name=security_id,
                          symbol='ES', security_id=security_id, security_exchange='CME'))
    gateway.terminal.poll()
    first.state.status = 'DISCONNECTED'
    gateway.terminal.poll()
    assert not gateway.terminal.books
    second = MarketSession()
    gateway._sessions['Market Data'] = second
    gateway.terminal.poll()
    assert {dict(fields)['48'] for _, fields in second.sent} == {'101', '102'}
    assert all(not book['timestamp'] for book in gateway.terminal.books.values())
