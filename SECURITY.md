<!--
SPDX-FileCopyrightText: Milan Pikula
SPDX-License-Identifier: EUPL-1.2
-->

# Security reporting

Report suspected security vulnerabilities privately to **[milan.pikula@ipsec.info](mailto:milan.pikula@ipsec.info)**. Include “MailIntel security report” in the subject.

Describe the affected version or commit, installation method, reproduction steps using synthetic data, and potential impact. Redact mailbox contents, credentials, API tokens, OAuth secrets, and personal information. Agree on a secure transfer method before sharing sensitive evidence.

The maintainer will coordinate investigation, fixes, and public disclosure with the reporter. Please keep vulnerability details private while a fix is prepared. Reporter credit is included with their consent and preferred name or handle.

## Supported versions

Security fixes target the latest released version and the development branch. During the pre-release period, use the latest development commit. Report issues affecting older versions too, and include whether you can reproduce them on the latest version.

## Security boundaries

MailIntel uses read-only mailbox connectors and scopes model operations to analysis and retrieval. The shared bearer token grants access to the installation. Operators protect the host, database, secrets, backups, and network access, and choose which model providers receive email content. See [Operations](doc/operations.md) for deployment and backup guidance.
