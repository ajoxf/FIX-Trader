"""Import the backup terminal's TT UAT settings without printing secrets."""
from pathlib import Path
import tomllib

from dotenv import set_key
from fixtrader.config import TraderConfig, VenueConfig


def main():
    root = Path(__file__).resolve().parent
    source = root.parent / '.streamlit' / 'secrets.toml'
    data = tomllib.loads(source.read_text(encoding='utf-8'))
    if data.get('application', {}).get('environment', 'UAT').upper() != 'UAT':
        raise SystemExit('This importer supports the backup TT UAT connection only.')
    order, md = data['fix_order'], data['fix_market_data']
    for section in (order, md):
        for key in ('host', 'port', 'sender_comp_id', 'target_comp_id', 'password'):
            if not section.get(key):
                raise SystemExit('Backup configuration is incomplete: ' + key)
    # Separate configuration avoids replacing an existing simulator desk/book.
    path = root / 'config.tt-uat.json'
    config = TraderConfig(path=str(path))
    config.settings['ALGO_MASTER_ENABLED'] = False
    config.settings['DATABASE_PATH'] = 'tt-uat.db'
    config.venues['TT-UAT'] = VenueConfig(
        name='TT-UAT', broker='TT', environment='UAT', fix_version='FIX.4.2',
        host=order['host'], port=order['port'],
        sender_comp_id=order['sender_comp_id'], target_comp_id=order['target_comp_id'],
        sender_sub_id=order.get('sender_sub_id', ''),
        on_behalf_of_sub_id=order.get('on_behalf_of_sub_id', ''),
        password_env='TT_UAT_ORDER_PASSWORD', account=order.get('account', ''),
        md_host=md['host'], md_port=md['port'],
        md_sender_comp_id=md['sender_comp_id'], md_target_comp_id=md['target_comp_id'],
        md_password_env='TT_UAT_MD_PASSWORD', use_tls=False,
    )
    if path.exists():
        raise SystemExit('config.tt-uat.json already exists; edit it instead of overwriting it.')
    env_path = root / '.env'
    env_path.touch(exist_ok=True)
    set_key(str(env_path), 'TT_UAT_ORDER_PASSWORD', order['password'])
    set_key(str(env_path), 'TT_UAT_MD_PASSWORD', md['password'])
    config.save()
    print('Imported TT UAT connection settings. Passwords stored only in .env.')


if __name__ == '__main__':
    main()
