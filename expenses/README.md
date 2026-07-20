# Expenses submission portal

Small Flask + SQLite web portal for employees to submit expenses (date, amount,
currency, description, receipt) and view/delete them, with Google Sign-In and
an email allowlist. Receipts are stored as blobs inside the SQLite database so
Litestream replication backs up everything continuously.

## Local development

```sh
cp config.example.json config.json   # fill in values
uv sync
EXPENSES_DEV_LOGIN=1 uv run python app.py
```

Open http://127.0.0.1:8000 and use the "Dev login" link (no Google needed).
`EXPENSES_DEV_LOGIN` must never be set in production.

## Google OAuth setup (one-time)

1. In [Google Cloud console](https://console.cloud.google.com/apis/credentials),
   create an OAuth client ID of type "Web application".
2. Add authorized JavaScript origins: `https://expenses.exe.xyz` (and
   `http://localhost:8000` for local testing against real Google login).
3. Put the client ID in `config.json` as `google_client_id`.
4. Fill in `allowed_emails` (employees) and `admin_emails` (you). Admins see
   everyone's expenses with a per-user filter; employees see only their own.
5. Generate `secret_key`: `python -c 'import secrets; print(secrets.token_hex(32))'`

## Deploy to exe.dev

```sh
ssh exe.dev new --name expenses
rsync -a --exclude .venv --exclude expenses.db ./ expenses.exe.xyz:/opt/expenses/
ssh expenses.exe.xyz
```

On the VM:

```sh
# Install uv and litestream
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -LsSf https://github.com/benbjohnson/litestream/releases/latest/download/litestream-linux-amd64.deb -o /tmp/ls.deb && dpkg -i /tmp/ls.deb

cd /opt/expenses
uv sync

# Edit litestream.yml (bucket, endpoint, region) and expenses.service
# (S3 credentials), then:
cp expenses.service /etc/systemd/system/
systemctl enable --now expenses

# If restoring from a previous backup (empty disk, new VM):
# litestream restore -config litestream.yml /opt/expenses/expenses.db
```

Back on your machine, expose it:

```sh
ssh exe.dev share port expenses 8000
ssh exe.dev share set-public expenses
```

Access control is the Google login + allowlist, so the public share is safe.

## Backups

Litestream continuously replicates `expenses.db` (including receipt blobs) to
the S3 bucket configured in `litestream.yml`; recovery point is ~1 second.
`litestream restore` recreates the database from the bucket on a fresh VM.
