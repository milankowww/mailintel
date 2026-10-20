# SPDX-FileCopyrightText: Milan Pikula
# SPDX-License-Identifier: EUPL-1.2

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mailintel import google_oauth
from mailintel.account_secrets import account_for_connector, account_for_response
from mailintel.api.routers import google_oauth as routes
from mailintel.connectors.base import ConnectorAuthenticationError, ConnectorConnectionError
from mailintel.connectors.imap import IMAPConnector
from mailintel.secrets import PasswordEncryptor


@pytest.fixture
def setup(tmp_path):
    encryptor = PasswordEncryptor(tmp_path / 'key')
    account = {'id': 1, 'auth_mode': 'google_oauth', 'settings_json': {'username': 'me@example.com'},
               'encrypted_password': None, 'password_encryption': None}
    def save(account_id, username, encrypted, encryption):
        if account['auth_mode'] != 'google_oauth' or account['settings_json']['username'] != username:
            return False
        account.update(encrypted_password=encrypted, password_encryption=encryption)
        return True
    repo = SimpleNamespace(get_mail_account=lambda _id: account, save_google_refresh_token=save)
    app = FastAPI()
    routes.register_routes(app, SimpleNamespace(repository=repo, password_encryptor=encryptor))
    client = TestClient(app, base_url='http://localhost:8765')
    config = {'client_id': 'example.apps.googleusercontent.com', 'client_secret': 'private-secret',
              'redirect_uri': 'http://localhost:8765/oauth/google/callback'}
    assert client.put('/api/google-oauth/config', json=config).status_code == 200
    return client, account, encryptor, config


def start(client):
    response = client.post('/api/accounts/1/google-oauth/start')
    assert response.status_code == 200, response.text
    return parse_qs(urlsplit(response.json()['authorization_url']).query)


def mock_google(monkeypatch, email='me@example.com', scope=google_oauth.MAIL_SCOPE):
    monkeypatch.setattr(routes, 'token_request', lambda data: {
        'refresh_token': 'refresh-secret', 'access_token': 'access-secret', 'scope': scope,
    })
    monkeypatch.setattr(routes.httpx, 'get', lambda *a, **kw: httpx.Response(
        200, json={'email': email, 'email_verified': True}, request=httpx.Request('GET', a[0])))


def test_config_encryption_redaction_and_redirect_validation(setup):
    client, _, encryptor, config = setup
    assert 'private-secret' not in (encryptor.key_file.parent / 'google-oauth.json').read_text()
    assert 'private-secret' not in client.get('/api/google-oauth/config').text
    assert client.put('/api/google-oauth/config', json={**config, 'client_secret': ''}).status_code == 200
    assert google_oauth.load_config(encryptor)['client_secret'] == 'private-secret'
    for redirect in ['https://mail.example.internal/oauth/google/callback', 'http://example.com/oauth/google/callback',
                     'https://example.com/elsewhere', 'https://example.com/oauth/google/callback?x=1']:
        assert client.put('/api/google-oauth/config', json={**config, 'redirect_uri': redirect}).status_code == 400


def test_consent_cookie_pkce_single_use_and_encrypted_refresh(setup, monkeypatch):
    client, account, encryptor, _ = setup
    params = start(client)
    assert params['access_type'] == ['offline']
    assert params['code_challenge_method'] == ['S256']
    mock_google(monkeypatch)
    url = '/oauth/google/callback?code=secret-code&state=' + params['state'][0]
    outsider = TestClient(client.app, base_url='http://localhost:8765')
    assert outsider.get(url).status_code == 400
    result = client.get(url, follow_redirects=False)
    assert result.headers['location'].endswith('google=connected')
    assert client.get(url).status_code == 400
    assert 'refresh-secret' not in str(account)
    assert account_for_response(account)['has_google_authorization'] is True
    assert account_for_response(account)['has_password'] is False
    assert 'encrypted_password' not in account_for_response(account)
    prepared = account_for_connector(account, encryptor)['settings_json']
    assert prepared['google_refresh_token'] == 'refresh-secret'
    assert 'password' not in prepared


@pytest.mark.parametrize('outcome,email,scope', [
    ('wrong-account', 'someone@example.com', google_oauth.MAIL_SCOPE),
    ('permission', 'me@example.com', 'openid email'),
])
def test_consent_rejects_wrong_identity_or_missing_scope(setup, monkeypatch, outcome, email, scope):
    client, account, _, _ = setup
    params = start(client)
    mock_google(monkeypatch, email, scope)
    result = client.get('/oauth/google/callback', params={'state': params['state'][0], 'code': 'code'}, follow_redirects=False)
    assert result.headers['location'].endswith('google=' + outcome)
    assert account['encrypted_password'] is None


def test_flow_expiry_cancellation_and_origin(setup, monkeypatch):
    client, account, _, _ = setup
    assert client.post('/api/accounts/1/google-oauth/start', headers={'Origin': 'https://other.example'}).status_code == 400
    params = start(client)
    result = client.get('/oauth/google/callback', params={'state': params['state'][0], 'error': 'access_denied'}, follow_redirects=False)
    assert result.headers['location'].endswith('google=cancelled')
    params = start(client)
    now = routes.time.monotonic()
    monkeypatch.setattr(routes.time, 'monotonic', lambda: now + 601)
    result = client.get('/oauth/google/callback', params={'state': params['state'][0], 'code': 'code'}, follow_redirects=False)
    assert result.headers['location'].endswith('google=expired')
    assert account['encrypted_password'] is None


def test_xoauth2_uses_verified_gmail_tls_and_empty_error_challenge(monkeypatch):
    monkeypatch.setattr(google_oauth, 'access_token', lambda settings: 'access-secret')
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {'host': 'imap.gmail.com', 'port': 993}
        def authenticate(self, mechanism, callback):
            assert mechanism == 'XOAUTH2'
            assert callback(b'') == b'user=me@example.com\x01auth=Bearer access-secret\x01\x01'
            assert callback(b'error') == b''
            return 'OK', []
    monkeypatch.setattr('mailintel.connectors.imap.imaplib.IMAP4_SSL', Client)
    connector = IMAPConnector({'auth_mode': 'google_oauth', 'username': 'me@example.com', 'host': 'evil.example',
                               'ssl': False, 'ignore_tls_errors': True})
    assert isinstance(connector._create_connection(), Client)


def test_refresh_errors_distinguish_revocation_from_transient_failure(monkeypatch):
    for status, error, expected in [(400, 'invalid_grant', ConnectorAuthenticationError), (503, 'unavailable', ConnectorConnectionError)]:
        monkeypatch.setattr(google_oauth.httpx, 'post', lambda *a, **kw: httpx.Response(status, json={'error': error}))
        with pytest.raises(expected):
            google_oauth.token_request({'refresh_token': 'never-log-this'})


def test_bridge_download(setup):
    response = setup[0].get('/google-oauth-localhost.py')
    assert response.status_code == 200
    assert 'ThreadingHTTPServer' in response.text


def test_account_changes_clear_oauth_credentials_in_postgres():
    from test_postgres_integration import build_repository
    from mailintel.models import MailAccount
    repo = build_repository()
    account = MailAccount(id=None, name='OAuth', provider_type='imap', auth_mode='google_oauth', secret_locator=None,
                          settings={'username': 'me@example.com'}, encrypted_password='ciphertext', password_encryption='test')
    try:
        account.id = repo.upsert_mail_account(account)
        account.encrypted_password = None
        repo.upsert_mail_account(account)
        assert repo.get_mail_account(account.id)['encrypted_password'] == 'ciphertext'
        account.settings['username'] = 'other@example.com'
        repo.upsert_mail_account(account)
        assert repo.get_mail_account(account.id)['encrypted_password'] is None
        assert not repo.save_google_refresh_token(account.id, 'me@example.com', 'stale', 'test')
        assert repo.save_google_refresh_token(account.id, 'other@example.com', 'new', 'test')
        account.auth_mode = 'config'
        repo.upsert_mail_account(account)
        assert repo.get_mail_account(account.id)['encrypted_password'] is None
    finally:
        repo.connection.close()


def test_background_refresh_does_not_need_callback_or_bridge(monkeypatch):
    calls = []
    def exchange(data):
        calls.append(data)
        return {'access_token': 'fresh-access'}
    monkeypatch.setattr(google_oauth, 'token_request', exchange)
    token = google_oauth.access_token({'google_refresh_token': 'saved-refresh', 'google_oauth_config': {
        'client_id': 'client', 'client_secret': 'secret', 'redirect_uri': 'http://localhost:8765/oauth/google/callback',
    }})
    assert token == 'fresh-access'
    assert calls == [{'grant_type': 'refresh_token', 'refresh_token': 'saved-refresh', 'client_id': 'client', 'client_secret': 'secret'}]


def test_localhost_bridge_tls_forwarding_and_browser_boundary(tmp_path):
    import datetime
    import socket
    import ssl
    import subprocess
    import sys
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
            .not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'ca.pem', tmp_path / 'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass
        def do_GET(self):
            self.send_response(303 if self.path.startswith('/oauth/') else 200)
            self.send_header('Location', '/#/mail-settings?google=connected')
            self.send_header('Set-Cookie', 'flow=example; HttpOnly; SameSite=Lax; Path=/oauth/google/callback')
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    upstream.socket = tls.wrap_socket(upstream.socket, server_side=True)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    helper = Path(google_oauth.__file__).parent / 'resources' / 'google_oauth_localhost.py'
    process = subprocess.Popen([sys.executable, str(helper), '--port', str(port), '--upstream',
                                f'https://localhost:{upstream.server_port}', '--ca-file', str(cert_path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with httpx.Client(base_url=f'http://localhost:{port}', trust_env=False) as client:
            for _ in range(50):
                try:
                    response = client.get('/api/health')
                    break
                except httpx.ConnectError:
                    time.sleep(.1)
            else:
                pytest.fail('Bridge did not start')
            assert response.json() == {'status': 'ok'}
            response = client.get('/oauth/google/callback?code=secret')
            assert response.status_code == 303
            assert response.headers['location'] == '/#/mail-settings?google=connected'
            assert 'HttpOnly' in response.headers['set-cookie']
            assert client.get('/', headers={'Origin': 'https://untrusted.example'}).status_code == 403
            assert client.get('/', headers={'Host': 'untrusted.example'}).status_code == 403
    finally:
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        upstream.shutdown()
        upstream.server_close()
    assert b'secret' not in stdout + stderr
