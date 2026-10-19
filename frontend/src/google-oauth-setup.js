export const GoogleOAuthSetup = {
  props: ['fetchJson'],
  data() {
    return { open: false, saving: false, status: '', config: { client_id: '', client_secret: '', redirect_uri: '', has_client_secret: false } };
  },
  methods: {
    async show() {
      this.open = true;
      this.status = '';
      try {
        const saved = await this.fetchJson('/api/google-oauth/config');
        this.config = { ...saved, client_secret: '', redirect_uri: saved.redirect_uri || `${location.origin}/oauth/google/callback` };
      } catch (error) { this.status = error.message; }
    },
    async save() {
      this.saving = true;
      try {
        await this.fetchJson('/api/google-oauth/config', { method: 'PUT', body: JSON.stringify(this.config) });
        this.config.client_secret = '';
        this.config.has_client_secret = true;
        this.status = 'Google setup saved. Close this guide, save your Gmail account, then click Connect Google.';
      } catch (error) { this.status = error.message; }
      finally { this.saving = false; }
    },
  },
  template: `
    <v-btn icon="mdi-information-outline" variant="text" size="small" aria-label="Google OAuth setup and help" title="Google OAuth setup and help" @click="show" />
    <v-dialog v-model="open" max-width="760" scrollable>
      <v-card title="Connect Gmail with Google OAuth">
        <v-card-text>
          <p>One-time setup for this MailIntel installation. Each mailbox then connects with its own Google consent.</p>
          <ol class="google-setup-steps">
            <li>Open <a href="https://console.cloud.google.com/auth/overview" target="_blank" rel="noopener noreferrer">Google Auth Platform</a> and create or select your project. Complete Branding with an app name, support email, and contact email.</li>
            <li>In Audience, use Internal only for users in your own Workspace organization. Otherwise choose External and add your Gmail address as a test user while testing.</li>
            <li>In Data Access, add <code>https://mail.google.com/</code>, <code>openid</code>, and <code>https://www.googleapis.com/auth/userinfo.email</code>. Google requires full mail permission for IMAP; MailIntel only reads mail. Email identity checks ensure you connect the intended mailbox.</li>
            <li>In Clients, create an OAuth client of type <strong>Web application</strong>. Add the exact callback URL below to Authorized redirect URIs. JavaScript origins are not needed for this server flow.</li>
            <li>Paste the client ID and secret below and save. They are encrypted locally. Leave the secret blank on later edits to keep it; changing the client ID requires a new secret and reconnecting accounts.</li>
            <li>Open MailIntel at the same origin as the callback URL. Save a Gmail account with your Google email address, click Connect Google, and grant access. Then choose mailboxes and sync.</li>
          </ol>
          <details class="mb-4">
            <summary>Private domains, localhost, and tunnels</summary>
            <p>Google rejects .internal and .local callback domains. Use an HTTPS domain you control with a public suffix, or access MailIntel through a localhost tunnel. The callback must reach this same MailIntel server in your browser.</p>
            <p><strong>The tunnel or bridge is only needed during consent.</strong> After connection, the server refreshes tokens directly with Google. Close it after setup; reopen it only if reconnection is needed.</p>
            <p>Without SSH, <a href="/google-oauth-localhost.py" download>download the temporary localhost bridge</a> and run it on the computer with your browser: <code>python3 google_oauth_localhost.py --upstream https://YOUR-MAILINTEL-HOST</code>. It uses Python’s standard library, binds only to 127.0.0.1, and verifies upstream HTTPS. If Python does not trust your private CA, add <code>--ca-file /path/to/ca.pem</code>. Then open <code>http://localhost:8765</code>, sign in with your MailIntel API token, and use the localhost callback below. Stop the bridge with Ctrl+C after Google connects.</p>
            <p>For a server reachable through SSH, forward a local port to its MailIntel port (normally 8000): <code>ssh -N -L 8765:127.0.0.1:8000 user@your-server</code>. Open <code>http://localhost:8765</code> and register <code>http://localhost:8765/oauth/google/callback</code>. Your server must provide SSH access. Sign in to MailIntel on that origin before connecting Google.</p>
          </details>
          <details class="mb-4">
            <summary>Token expiry and troubleshooting</summary>
            <p>External projects in Testing normally issue refresh tokens lasting seven days. For ongoing personal use, review Google's publishing options; public apps may require restricted-scope verification. Moving to Production does not itself grant verification. Workspace administrators may also need to allow the app and IMAP access.</p>
            <p>Redirect mismatch: match the registered URL exactly. Session expired: start Connect Google again; flows last ten minutes and restart cancels them. Wrong account: select the exact mailbox address entered in MailIntel. Revoked or expired access: use Reconnect Google, then retry sync. Changing hosts requires updating both Google's registered URI and this setting.</p>
            <p><a href="https://developers.google.com/workspace/gmail/imap/xoauth2-protocol" target="_blank" rel="noopener noreferrer">Google IMAP scope requirements</a> · <a href="https://developers.google.com/identity/protocols/oauth2/web-server" target="_blank" rel="noopener noreferrer">Google OAuth setup</a></p>
          </details>
          <v-text-field v-model="config.redirect_uri" label="Callback URL" placeholder="https://mail.example.com/oauth/google/callback" />
          <v-text-field v-model="config.client_id" label="Google client ID" autocomplete="off" />
          <v-text-field v-model="config.client_secret" label="Google client secret" type="password" autocomplete="new-password" :hint="config.has_client_secret ? 'Saved. Leave blank to keep it.' : 'From the Google Web application client.'" persistent-hint />
          <p role="status" class="mt-4">{{ status }}</p>
        </v-card-text>
        <v-card-actions><v-btn color="primary" :loading="saving" @click="save">Save Google setup</v-btn><v-btn @click="open = false">Close</v-btn></v-card-actions>
      </v-card>
    </v-dialog>
  `,
};
