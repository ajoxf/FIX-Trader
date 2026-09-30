import json
import threading
import time
from types import SimpleNamespace

from fixtrader import atomicfile
from fixtrader.quote_stream import QuotePublisher, events
from tests.test_manual_terminal import terminal


def test_quote_publisher_writes_only_market_state(terminal, tmp_path):
    terminal.books['101'].update(bid=10, ask=10.25, timestamp='2026-09-09T00:00:00+00:00')
    path=tmp_path/'status.json'
    publisher=QuotePublisher(terminal, path)
    publisher.publish()
    payload=json.loads((tmp_path/'status.json.quotes.json').read_text())
    assert payload['source']=='TT_FIX_UAT' and payload['simulated'] is False
    assert payload['connected'] is True
    assert payload['quotes']['101']['bid']==10
    assert 'entries' not in payload['quotes']['101']
    assert 'orders' not in payload


def test_market_message_signals_quote_publisher(terminal):
    terminal.quote_changed.clear()
    raw=f'35=W\x01262={terminal.subscriptions["101"]}\x01268=2\x01269=0\x01270=10\x01269=1\x01270=11\x01'
    terminal.on_message('Market Data', {'35':'W','34':'41'}, raw)
    assert terminal.quote_changed.is_set()
    assert terminal.books['101']['fix_message_type']=='W'
    assert terminal.books['101']['fix_sequence']=='41'


def test_stream_receives_atomic_quote_replacement(tmp_path):
    path = tmp_path / 'quotes.json'
    atomicfile.write_json(str(path), {'published_ms': 1, 'quotes': {}}, durable=False)
    stream = events(str(path))
    assert next(stream).startswith('retry:')
    assert json.loads(next(stream).split('data: ', 1)[1])['published_ms'] == 1

    def replace():
        time.sleep(0.02)
        atomicfile.write_json(str(path), {'published_ms': 2, 'quotes': {'101': {'bid': 10}}}, durable=False)

    worker = threading.Thread(target=replace)
    worker.start()
    try:
        frame = json.loads(next(stream).split('data: ', 1)[1])
        assert frame['published_ms'] == 2
        assert frame['quotes']['101']['bid'] == 10
        assert frame['server_sent_ms'] >= 2
    finally:
        worker.join()
        stream.close()
