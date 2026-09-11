import json

from fixtrader.fix_audit import FixAuditLog


def test_fix_audit_redacts_credentials_and_filters(tmp_path):
    log = FixAuditLog(tmp_path)
    log.write(level='INFO', category='FIX Session', session='Market Data',
              direction='OUT', event='Logon', sequence='1', details={},
              raw='35=A|95=6|96=secret|553=user|554=pass|')
    raw = log.read()[0]['raw']
    assert 'secret' not in raw and 'pass' not in raw and 'user' not in raw
    assert raw.count('[redacted]') == 4
    assert len(log.read(category='FIX Session', search='Logon')) == 1
    assert log.read(category='Orders') == []


def test_fix_log_page_and_api(tmp_path):
    from fixtrader.config import TraderConfig
    from fixtrader.webapp import create_app
    config = tmp_path / 'config.json'
    TraderConfig(path=str(config)).save()
    app = create_app(str(config), str(tmp_path / 'status.json'),
                     str(tmp_path / 'commands.jsonl'), str(tmp_path / 'results.json'))
    app.config['TESTING'] = True
    client = app.test_client()
    response = client.get('/logs')
    assert response.status_code == 200
    assert b'FIX logs' in response.data
    assert client.get('/api/fix-logs').status_code == 200
