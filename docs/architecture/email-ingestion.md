# Email Ingestion

> **Status:** Active — added 2026-07-06 (Phase 8a)
> **Audience:** Operators, administrators, self-hosted users

## Overview

DocMind can ingest email from IMAP accounts (Gmail, Outlook, self-hosted
servers) and convert messages and attachments into searchable documents.
Each account is polled on a configurable interval, new emails are
deduplicated by Message-ID, and attachments are extracted using the same
`Extractor` pipeline that handles uploaded files.

Email ingestion is **disabled by default**. Enable it when you want
DocMind to automatically pull documents from one or more mailboxes.

## Quick start

```bash
# Enable email ingestion
export DOCMIND_EMAIL_ENABLED=true

# Configure your first account (Gmail example with app password)
export DOCMIND_EMAIL_ACCOUNT_0_NAME="Personal Gmail"
export DOCMIND_EMAIL_ACCOUNT_0_HOST="imap.gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PORT="993"
export DOCMIND_EMAIL_ACCOUNT_0_USERNAME="you@gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="abcd efgh ijkl mnop"

# Optional: adjust poll interval (default: 600 seconds = 10 minutes)
export DOCMIND_EMAIL_POLL_INTERVAL="300"
```

After restarting DocMind, the email worker begins polling in the
background. Logs appear at INFO level:

```
Email worker started: 1 account(s) enabled, poll interval 300s
```

## Behaviour

### Polling cycle

The email worker runs in the same process as the web server, inside
FastAPI's `lifespan` context. It:

1. Reads all enabled accounts from configuration (env vars or DB).
2. Polls each account sequentially, fetching unseen emails since the
   last successful sync.
3. For each new email: deduplicates → parses MIME structure → extracts
   body text → extracts attachments → creates documents → logs result.
4. Applies the configured post-fetch action (mark as seen, delete, or
   move to folder).
5. Sleeps for the configured poll interval, then repeats.

If an account's poll fails (network error, auth failure, IMAP timeout),
the error is logged and the worker moves to the next account. The failed
account is retried on the next cycle.

### Deduplication

DocMind uses a 3-layer composite deduplication strategy to avoid
importing the same email twice:

| Layer  | Key                       | When it applies              |
|--------|---------------------------|------------------------------|
| 1      | Message-ID header hash    | Primary — nearly all emails  |
| 2      | Account + folder + UID    | Fallback for missing IDs     |
| 3      | Content hash              | Last resort                   |

The ingestion log (`email_ingestion_log` table) stores every processed
email with its dedup key. Before processing a new email, the ingestor
checks all three layers. A match at any layer skips the email.

### Document creation

Each ingested email can produce multiple documents:

- The email body becomes a document with `source_type = "email"`.
- Each supported attachment (PDF, DOCX, TXT, etc.) becomes a separate
  document.
- All documents from the same email share a `thread_id` in metadata,
  enabling future thread-based grouping.

Unsupported attachment types are logged and skipped — they do not block
the email from being processed.

### Post-fetch actions

The default action is `mark_seen`, which marks the email as read on the
IMAP server after successful ingestion. Other options:

| Action         | Description                                    |
|----------------|------------------------------------------------|
| `mark_seen`    | Mark as read. Safe default.                    |
| `delete`       | Delete from server after ingestion.            |
| `move_folder`  | Move to a specified folder (deferred to Phase 8d). |

## Configuration reference

Email ingestion is configured through **environment variables**. There
are no YAML settings — env vars are the canonical config source.

### Global settings

| Variable                       | Default | Description                              |
|--------------------------------|---------|------------------------------------------|
| `DOCMIND_EMAIL_ENABLED`        | `false` | Enable or disable the email worker.      |
| `DOCMIND_EMAIL_POLL_INTERVAL`  | `600`   | Seconds between poll cycles (all accounts). |

### Per-account settings

Accounts are defined via indexed env vars using the pattern
`DOCMIND_EMAIL_ACCOUNT_<N>_<FIELD>`. Indexes start at 0 and must be
contiguous.

| Variable                                          | Default          | Description                        |
|---------------------------------------------------|------------------|------------------------------------|
| `DOCMIND_EMAIL_ACCOUNT_<N>_NAME`                  | (required)       | Human-readable label.              |
| `DOCMIND_EMAIL_ACCOUNT_<N>_HOST`                  | (required)       | IMAP server hostname.              |
| `DOCMIND_EMAIL_ACCOUNT_<N>_PORT`                  | `993`            | IMAP port.                         |
| `DOCMIND_EMAIL_ACCOUNT_<N>_USE_SSL`               | `true`           | Use SSL/TLS for the connection.    |
| `DOCMIND_EMAIL_ACCOUNT_<N>_USERNAME`              | (required)       | IMAP login username.               |
| `DOCMIND_EMAIL_ACCOUNT_<N>_PASSWORD`              | (required)       | IMAP password or app password.     |
| `DOCMIND_EMAIL_ACCOUNT_<N>_FOLDER`                | `INBOX`          | Mailbox folder to poll.            |
| `DOCMIND_EMAIL_ACCOUNT_<N>_ACTION_AFTER_FETCH`    | `mark_seen`      | Post-fetch action.                 |
| `DOCMIND_EMAIL_ACCOUNT_<N>_BODY_HANDLING`         | `save_with_attachments` | How to handle email body.  |
| `DOCMIND_EMAIL_ACCOUNT_<N>_ATTACHMENT_WHITELIST`  | (empty)          | Comma-separated globs to include.  |
| `DOCMIND_EMAIL_ACCOUNT_<N>_ATTACHMENT_BLACKLIST`  | (empty)          | Comma-separated globs to exclude.  |
| `DOCMIND_EMAIL_ACCOUNT_<N>_ENABLED`               | `true`           | Enable or disable this account.    |

### Provider-specific examples

**Gmail (requires App Password):**

```bash
export DOCMIND_EMAIL_ACCOUNT_0_NAME="Work Gmail"
export DOCMIND_EMAIL_ACCOUNT_0_HOST="imap.gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PORT="993"
export DOCMIND_EMAIL_ACCOUNT_0_USERNAME="you@gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="abcd efgh ijkl mnop"
```

1. Enable 2-Step Verification on your Google Account.
2. Generate an App Password at https://myaccount.google.com/apppasswords.
3. Use that 16-character password (spaces optional) as the value.

**Outlook / Office 365 (requires App Password or OAuth2):**

```bash
export DOCMIND_EMAIL_ACCOUNT_0_NAME="Work Outlook"
export DOCMIND_EMAIL_ACCOUNT_0_HOST="outlook.office365.com"
export DOCMIND_EMAIL_ACCOUNT_0_PORT="993"
export DOCMIND_EMAIL_ACCOUNT_0_USERNAME="you@company.com"
export DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="your-app-password"
```

**Self-hosted IMAP (Dovecot, etc.):**

```bash
export DOCMIND_EMAIL_ACCOUNT_0_NAME="Local Mail"
export DOCMIND_EMAIL_ACCOUNT_0_HOST="mail.example.com"
export DOCMIND_EMAIL_ACCOUNT_0_PORT="993"
export DOCMIND_EMAIL_ACCOUNT_0_USERNAME="docmind@example.com"
export DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="mailbox-password"
```

### Multiple accounts

Add more accounts by incrementing the index:

```bash
# Account 0
export DOCMIND_EMAIL_ACCOUNT_0_NAME="Personal"
export DOCMIND_EMAIL_ACCOUNT_0_HOST="imap.gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_USERNAME="personal@gmail.com"
export DOCMIND_EMAIL_ACCOUNT_0_PASSWORD="..."

# Account 1
export DOCMIND_EMAIL_ACCOUNT_1_NAME="Work"
export DOCMIND_EMAIL_ACCOUNT_1_HOST="outlook.office365.com"
export DOCMIND_EMAIL_ACCOUNT_1_USERNAME="work@company.com"
export DOCMIND_EMAIL_ACCOUNT_1_PASSWORD="..."
```

Indexes must be contiguous starting from 0. Gaps cause parsing to stop
at the gap — account 2 will be ignored if account 1 is missing.

## Security

### Credential storage — Fernet encryption

**IMAP passwords are encrypted at rest using Fernet symmetric encryption.**
Passwords are never stored as plaintext in the database — they are
encrypted before write and decrypted only when needed for IMAP
authentication.

| Aspect            | Current state                          |
|-------------------|----------------------------------------|
| Storage           | Fernet-encrypted in `email_accounts` table |
| Key management    | `DOCMIND_EMAIL_ENCRYPTION_KEY` env var    |
| DB compromise     | Credentials are encrypted at rest         |
| Encryptor design  | Per-Database instance, not module singleton |

**How it works:**

- A Fernet key is derived from the `DOCMIND_EMAIL_ENCRYPTION_KEY`
  environment variable.
- Each `Database` instance owns its encryptor (`db._encryptor`),
  preventing cross-database key leakage in tests (commit 15d6075).
- All email account CRUD methods encrypt on write, decrypt on read.
- If `DOCMIND_EMAIL_ENCRYPTION_KEY` is not set, credentials are stored
  in plaintext with a warning — useful for development and testing.

**What this means in practice:**

- Someone with filesystem access to `docmind.db` sees only encrypted
  password values, not the original passwords.
- Backups of `docmind.db` do not contain plaintext credentials.
- The encryption key itself must be protected — treat it like a master
  password. Store it in a secrets manager or `.env` file with
  restricted permissions.

**Mitigations you should still apply:**

1. **Use app-specific passwords, not your primary account password.**
   Gmail and Outlook both support app passwords that are scoped to IMAP
   only and can be revoked independently.

2. **Restrict filesystem access to `docmind.db`.**
   ```bash
   chmod 600 docmind.db
   ```

3. **Protect the encryption key.** Store it in a secure location:
   ```bash
   # Generate a strong random key
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

   # Store in a restricted .env file
   echo 'DOCMIND_EMAIL_ENCRYPTION_KEY=...' >> .env
   chmod 600 .env
   ```

4. **Do not commit `docmind.db` or `.env` to version control.**

5. **Rotate app passwords periodically.** Treat them like API keys.

### IMAP connection security

All IMAP connections use SSL/TLS by default (port 993,
`use_ssl=true`). DocMind uses Python's `ssl.create_default_context()`
with certificate validation enabled. Connections to servers with
invalid or self-signed certificates are rejected.

To connect to a server with a self-signed certificate (not
recommended for production), set `use_ssl` to `false` and use a
non-standard port. This disables TLS entirely — use only on trusted
local networks.

### Attachment safety

Attachments are processed through the same `Extractor` sandbox as
uploaded files. The `ProcessPoolExecutor` isolation limits the blast
radius of a malicious document. Attachment size is capped by
`DocumentLimits.max_file_size_bytes` (default: 100 MB).

### PII in email content

Email bodies may contain personally identifiable information. DocMind
respects `SanitizerConfig.redact_pii` if enabled, applying the same
redaction rules used for uploaded documents. Headers (Subject, From, To)
are always stored for threading and search — consider this when
ingesting sensitive correspondence.

## Monitoring

The email worker logs at several levels:

| Level   | Event                                                      |
|---------|------------------------------------------------------------|
| INFO    | Worker started, poll cycle begin/end, emails processed     |
| WARNING | Individual email parse failure, unsupported attachment     |
| ERROR   | Account poll failure (network, auth, IMAP error)           |

Monitor these log lines to detect:

- Authentication failures (check your app password hasn't expired)
- Network issues (IMAP server unreachable)
- Accounts that consistently produce zero new emails (may indicate a
  folder mismatch or IMAP server quirk)

Example log output during normal operation:

```
Email worker started: 2 account(s) enabled, poll interval 300s
Polling account 'Personal Gmail' (imap.gmail.com)...
Account 'Personal Gmail': 3 new emails, 5 documents created
Polling account 'Work Outlook' (outlook.office365.com)...
Account 'Work Outlook': connection failed: AUTHENTICATIONFAILED
```

## Testing IMAP connectivity

You can verify your IMAP credentials without waiting for the next poll
cycle. Use the API endpoint:

```bash
curl -X POST http://localhost:9980/api/v1/email-accounts/1/test
```

A successful response:

```json
{
  "success": true,
  "message": "Connected to imap.gmail.com:993, 42 messages in INBOX"
}
```

A failure response:

```json
{
  "success": false,
  "error": "AUTHENTICATIONFAILED: Invalid credentials"
}
```

You can also test manually with `openssl`:

```bash
openssl s_client -connect imap.gmail.com:993 -crlf -quiet
```

## Architecture

The email ingestion pipeline lives in `src/core/email_ingestor.py`:

- **`EmailIngestor` class** — Core service that polls IMAP, parses MIME,
  extracts attachments, creates documents, and logs results. All IMAP
  operations are synchronous (`imaplib`) wrapped in `asyncio.to_thread`
  to avoid blocking the event loop.
- **`EmailAccountConfig`** — Dataclass representing a single account's
  configuration. Loaded from env vars at startup.
- **`EmailConfig`** — Top-level dataclass holding the global enabled flag
  and poll interval.
- **`email_polling_worker()`** — Long-lived async background task started
  in `lifespan()`. Polls all enabled accounts in sequence on the
  configured interval.

The worker runs in the same process as the web server (single-worker
asyncio deployment). No separate process or external scheduler is
required.

### Database tables

| Table                  | Purpose                                         |
|------------------------|-------------------------------------------------|
| `email_accounts`       | Account configuration (mirrors env var settings). |
| `email_ingestion_log`  | Deduplication store and audit trail.             |

Both tables are created automatically on first startup when
`DOCMIND_EMAIL_ENABLED=true`.

### API endpoints

| Method | Path                                  | Description             |
|--------|---------------------------------------|-------------------------|
| GET    | `/api/v1/email-accounts`              | List all accounts.      |
| POST   | `/api/v1/email-accounts`              | Create an account.      |
| GET    | `/api/v1/email-accounts/{id}`         | Get account details.    |
| PUT    | `/api/v1/email-accounts/{id}`         | Update an account.      |
| DELETE | `/api/v1/email-accounts/{id}`         | Delete an account.      |
| POST   | `/api/v1/email-accounts/{id}/sync`    | Trigger manual sync.    |
| GET    | `/api/v1/email-accounts/{id}/logs`    | Get ingestion logs.     |
| POST   | `/api/v1/email-accounts/{id}/test`    | Test IMAP connection.   |

## Limitations

- **No OAuth2 support.** IMAP authentication is username/password only.
  For providers that require OAuth2 (modern Microsoft 365 tenants
  without legacy auth), use an app password if available. Full OAuth2
  support is a future enhancement.

- **Sequential polling.** Accounts are polled one at a time. With many
  accounts and a short poll interval, a slow account can delay the
  others. This is intentional for MVP simplicity — concurrent polling
  can be added if needed.

- **No push/IDLE support.** DocMind polls on a timer rather than using
  IMAP IDLE for real-time notifications. The minimum poll interval is
  effectively the fastest you can get new emails. For most document
  ingestion use cases (not real-time messaging), a 5-10 minute interval
  is sufficient.

- **No email reply or send capability.** Email ingestion is one-way:
  read from IMAP, create documents. DocMind does not send emails.

## Testing

Email ingestion is tested in `tests/test_email_ingestor.py`, covering:

- Email parsing from fixture `.eml` files (plain text, HTML, multipart)
- Attachment extraction and filtering (whitelist/blacklist globs)
- Body extraction (text/plain preference, HTML-to-text fallback)
- Thread ID computation from In-Reply-To, References, and Message-ID
  headers
- Deduplication logic across all three layers
- IMAP mock integration (mock `imaplib.IMAP4_SSL` responses)
- Error handling (connection failure, auth failure, malformed emails)

Run the email ingestion tests in isolation:

```bash
pytest tests/test_email_ingestor.py -v
```
