# User guide

## Connect and synchronize mail

In Mail Settings, create an account with its server address, authentication method, and credentials. IMAP supports password credentials and Gmail OAuth. Exchange uses EWS; your server must support the authentication method selected in the account settings. MailIntel reads messages while preserving mailbox flags.

Save the account, choose **Choose mailboxes**, and select the folders to monitor. **Only ingest last N days** limits unread ingestion to recent messages. Leave it blank for the unrestricted unread-first window. Start sync and inspect processing status for progress or authentication errors. Messages become searchable after analysis and embeddings finish.

For Gmail, follow [Google OAuth](google-oauth.md). Password accounts store encrypted credentials. A secret locator can refer to an environment variable available to the backend process.

## Personalize triage

Use Triage Settings to describe your role, organization, collaborators, and communication preferences. Define interest profiles for subjects you want to follow. Their descriptions and priorities help MailIntel match and rank messages. Review the dashboard's summaries and explanations alongside the original messages.

## Explore topics and starred messages

Topics group related messages using their embeddings. Open a topic to inspect its messages, edit its label, or request a new label. Topics appear as background clustering catches up with indexing.

Use message stars to bookmark messages inside MailIntel. Stars persist in the application database independently of mail-server flags.

## Ask questions

Use search to ask about indexed mail. Search creates a background job, plans retrieval, combines text and vector matching, and prepares an answer from retrieved evidence. Inspect the returned sources and coverage information. An answer is limited by the selected mailboxes, sync window, completed indexing, and search limits. Use the original mailbox to verify questions beyond that coverage.

A failed model or retrieval step reports a search failure. Check the indicated stage, provider configuration, and indexing status before retrying. Long messages can require additional compression calls and take longer.

## Manage processing

The queue view shows pending and active work. Use pause, resume, priority, and delete controls for individual jobs. Global AI pause interrupts model work; local non-model tasks can continue. Lower numeric queue priorities run first. Provider concurrency determines how many model calls can run at once.

Maintenance offers rebuilds and resets for derived data. Read the action's confirmation carefully: resetting summaries or processing can require substantial re-analysis. Use Config Backup before changing account or model configuration, and maintain a full database backup for mail and derived results.
