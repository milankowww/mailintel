# Gmail over IMAP with Google OAuth

Choose **Gmail / Google OAuth** in Mail settings. The adjacent info button contains the setup tutorial and installation-wide OAuth client settings. Save the account, connect Google, choose mailboxes, then sync.

The installation uses a Google **Web application** OAuth client with an exact `/oauth/google/callback` redirect URI. IMAP requires `https://mail.google.com/`; OpenID/email scopes verify that consent belongs to the configured mailbox. Review Google’s current publishing, verification, and token-lifetime requirements when configuring ongoing access.

## Create your OAuth client

1. Open Google Cloud Console and select or create a project. In Google Auth Platform, complete Branding with your app name and contact details.
2. Configure Audience for your Workspace organization or external users. Add your mailbox as a test user when using a testing project.
3. In Data Access, add `https://mail.google.com/`, `openid`, and `https://www.googleapis.com/auth/userinfo.email`.
4. Create a **Web application** client under Clients. Register the exact callback URL for your MailIntel origin, for example `https://mail.example.com/oauth/google/callback`.
5. Open the Google setup help in Mail Settings. Save the client ID, secret, and matching callback URL. Then save your Gmail account with its Google email address and choose **Connect Google**.
6. Complete consent, choose monitored mailboxes, and start sync. Use the same browser origin for setup and the callback.

For local access, register `http://localhost:8000/oauth/google/callback` and open MailIntel through `http://localhost:8000`. For a private remote server, use the bridge below.

## Private installations

Google rejects `.internal` web callbacks. A temporary localhost bridge lets the browser complete consent without a public domain or permanent relay:

1. Download `/google-oauth-localhost.py` from MailIntel's setup help.
2. On the browser's computer, run `python3 google_oauth_localhost.py --upstream https://mail.example.com` (replace the origin for another installation). The bridge uses the Python 3.10+ standard library. If Python needs the private CA, pass `--ca-file /path/to/ca.pem`.
3. Open `http://localhost:8765/#/mail-settings`. Authenticate with the same MailIntel API token used on the normal origin.
4. Register `http://localhost:8765/oauth/google/callback` in the Google web client and save that same callback in MailIntel's Google setup.
5. Complete Connect Google through localhost, then stop the bridge with Ctrl+C. Use the usual private address thereafter.

The bridge listens on 127.0.0.1, validates Host/Origin, verifies upstream HTTPS, and keeps request URLs private. The private server must be reachable from the browser's computer. SSH port forwarding to the app's port is an alternative.

**Background sync runs directly on the server.** The server exchanges its encrypted refresh token directly with Google when opening IMAP connections. Reopen the bridge only to reconnect after revocation/expiry or to connect another account. Reconnect when Google requires renewed authorization.

## Implementation and operations

The IMAP connector uses XOAUTH2 for `auth_mode=google_oauth`, fixes the server to `imap.gmail.com:993`, and enforces certificate verification. Existing IMAP ingestion and password authentication are shared.

The existing encrypted account-secret column (`encrypted_password`) holds the refresh token for Google accounts; this mode uses it exclusively as an OAuth refresh token. Changing to/from Google authentication or changing the Google email address clears the old credential. API responses expose only authorization presence. The installation client configuration is encrypted in `google-oauth.json` beside the encryption key. Keep the existing persistent secrets volume. Encrypted configuration export/import also includes this configuration and account refresh tokens.

Consent state is single-use, bound to an HttpOnly SameSite cookie, expires after ten minutes, and uses PKCE. Restarting the app cancels pending consent. The callback checks granted mail scope and Google-verified email identity before saving the refresh token. App callback access logs omit the query string; any separate proxy logging should likewise exclude OAuth query strings.

Revoked credentials produce a reconnect instruction and use the existing authentication-failure sync pause. Temporary token-service failures remain connection errors. After reconnecting, retry sync with the existing UI control.

References: [Google IMAP OAuth](https://developers.google.com/workspace/gmail/imap/xoauth2-protocol), [web authorization](https://developers.google.com/identity/protocols/oauth2/web-server), [token expiry](https://developers.google.com/identity/protocols/oauth2/).
