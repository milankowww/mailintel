# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2
"""Check a freshly started image using only synthetic, empty application state."""
from importlib.metadata import files, metadata
from pathlib import Path
import re
import time

import httpx
from mailintel.auth.token import get_or_create_api_token
from mailintel.settings import get_settings

assert metadata('mailintel')['License-Expression'] == 'EUPL-1.2'
installed = files('mailintel')
license_file = next(p for p in installed if str(p).endswith('.dist-info/licenses/LICENSE'))
assert Path('/usr/share/licenses/mailintel/LICENSE').read_bytes() == license_file.locate().read_bytes()
with httpx.Client(base_url='http://127.0.0.1:8000', timeout=5, trust_env=False) as client:
    for attempt in range(30):
        try:
            if client.get('/api/health').status_code == 200:
                break
        except httpx.TransportError:
            pass
        time.sleep(1)
    assert client.get('/api/health').json() == {'status': 'ok'}
    assert client.get('/api/accounts').status_code == 401
    token = get_or_create_api_token(get_settings())
    assert client.get('/api/accounts', headers={'Authorization': f'Bearer {token}'}).status_code == 200
    page = client.get('/')
    assert page.status_code == 200
    asset = re.search(r'src="([^"]+\.js)"', page.text).group(1)
    assert client.get(asset).status_code == 200
print('Image startup, license packaging, API authentication, and frontend assets verified')
