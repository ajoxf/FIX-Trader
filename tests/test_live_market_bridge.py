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
