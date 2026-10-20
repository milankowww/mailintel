<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# User guide

MailIntel provides summaries, priorities, topics, and answers for you to review alongside their source messages. Mailbox access is read-only; you decide what action to take in your email client.

The screenshots use illustrative correspondence and sanitized identities. [Browse all screenshots](screenshots/README.md) for a visual overview.

- [Connect and synchronize mail](#connect-and-synchronize-mail)
- [Choose models](#choose-models)
- [Personalize triage](#personalize-triage)
- [Review priorities and interests](#review-priorities-and-interests)
- [Explore topics and starred messages](#explore-topics-and-starred-messages)
- [Read daily, weekly, and monthly summaries](#read-daily-weekly-and-monthly-summaries)
- [Ask questions](#ask-questions)
- [Manage processing](#manage-processing)

## Connect and synchronize mail

In Mail Settings, create an account with its server address, authentication method, and credentials. IMAP supports password credentials and Gmail OAuth. Exchange uses EWS; your server must support the authentication method selected in the account settings. MailIntel reads messages while preserving mailbox flags.

Save the account, choose **Choose mailboxes**, and select the folders to monitor. **Only ingest last N days** limits unread ingestion to recent messages. Leave it blank for the unrestricted unread-first window. Start sync and inspect processing status for progress or authentication errors. Messages become searchable after analysis and embeddings finish.

For Gmail, follow [Google OAuth](google-oauth.md). Password accounts store encrypted credentials. A secret locator can refer to an environment variable available to the backend process.

![Mail accounts: Saved accounts with mailbox selection, sync controls, and progress.](screenshots/11-settings-mail-accounts.png)

*Saved accounts with mailbox selection, sync controls, and progress.*

## Choose models

In **LLM Settings**, add and enable presets for your model providers. Configure text generation and embeddings, check connectivity, and select the capabilities each preset serves. Choose a canonical embedding model and use it consistently for indexing and search.

The example shows local model presets. Use the endpoint and model names available in your installation; see [Connect a model service](installation.md#connect-a-model-service) for local and cloud setup, including access from Docker.

![Model settings: Model presets for analysis, embeddings, and search.](screenshots/13-settings-llm.png)

*Model presets for analysis, embeddings, and search.*

## Personalize triage

Use Triage Settings to describe your role, organization, collaborators, and communication preferences. Define interest profiles for subjects you want to follow. Their descriptions and priorities help MailIntel match and rank messages. Review the dashboard's summaries and explanations alongside the original messages.

![Triage settings: Personal context, communication preferences, and saved interest profiles.](screenshots/12-settings-triage.png)

*Personal context, communication preferences, and saved interest profiles.*

## Review priorities and interests

Open **Priority Triage** to review ranked messages. Each card presents a summary and an explanation of its score. Select a date range to focus on the period you want to review.

![Priority triage: Messages ranked with a summary and an explanation of their importance.](screenshots/01-priority-triage.png)

*Messages ranked with a summary and an explanation of their importance.*

Open **Interest Profiles** to browse messages matched to each of your interests. Review the match and importance scores alongside the message summary.

![Interest profiles: Matching messages grouped under personal interests such as family and travel.](screenshots/03-interest-profiles.png)

*Matching messages grouped under personal interests such as family and travel.*

## Explore topics and starred messages

Topics group related messages using their embeddings. Open a topic to inspect its messages, edit its label, or request a new label. Topics appear as background clustering catches up with indexing.

![Topics: Related messages grouped into topics for the selected date range.](screenshots/04-topics.png)

*Related messages grouped into topics for the selected date range.*

Use message stars to bookmark messages inside MailIntel. Stars persist in the application database independently of mail-server flags.

![Starred messages: Bookmarked conference and travel messages collected in one view.](screenshots/02-starred.png)

*Bookmarked conference and travel messages collected in one view.*

## Read daily, weekly, and monthly summaries

Use the **Daily**, **Weekly**, and **Monthly** tabs to review correspondence over different periods. Select a day, week, or month in the picker. Summaries are grouped by mailbox; **Open messages** lets you inspect the evidence behind each item.

### Daily

![Daily summary: A selected day’s mailbox summaries with links to supporting messages.](screenshots/05-daily-summary.png)

*A selected day’s mailbox summaries with links to supporting messages.*

### Weekly

![Weekly summary: A week’s correspondence summarized by mailbox.](screenshots/06-weekly-summary.png)

*A week’s correspondence summarized by mailbox.*

### Monthly

![Monthly summary: A month’s correspondence summarized by mailbox.](screenshots/07-monthly-summary.png)

*A month’s correspondence summarized by mailbox.*

## Ask questions

Use search to ask about indexed mail. Search creates a background job, plans retrieval, combines text and vector matching, and prepares an answer from retrieved evidence. Inspect the returned sources and coverage information. An answer is limited by the selected mailboxes, sync window, completed indexing, and search limits. Use the original mailbox to verify questions beyond that coverage.

![Search answer and sources: An event question answered alongside the messages supporting the answer.](screenshots/09-cybertech-search.png)

*An event question answered alongside the messages supporting the answer.*

Use the history button beside **Search** to revisit previous questions and their results.

![Search history: Previous questions available from the history button beside Search.](screenshots/08-search-history.png)

*Previous questions available from the history button beside Search.*

Expand a message’s preview to compare its content with the generated summary. This example shows travel details in the message card.

![Expanded message: A travel message with its summary, preview, and scoring explanation.](screenshots/10-rome-itinerary.png)

*A travel message with its summary, preview, and scoring explanation.*

A failed model or retrieval step reports a search failure. Check the indicated stage, provider configuration, and indexing status before retrying. Long messages can require additional compression calls and take longer.

## Manage processing

The queue view shows pending and active work. Use pause, resume, priority, and delete controls for individual jobs. Global AI pause interrupts model work; local non-model tasks can continue. Lower numeric queue priorities run first. Provider concurrency determines how many model calls can run at once.

![Troubleshooting: Job filters, worker status, and recent processing events.](screenshots/15-settings-troubleshooting.png)

*Job filters, worker status, and recent processing events.*

Maintenance offers rebuilds and resets for derived data. Read the action's confirmation carefully: resetting summaries or processing can require substantial re-analysis. Use Config Backup before changing account or model configuration, and maintain a full database backup for mail and derived results.

![Maintenance: Controls for rebuilding derived data and resetting configuration.](screenshots/14-settings-maintenance.png)

*Controls for rebuilding derived data and resetting configuration.*
