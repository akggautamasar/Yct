# Oracle ARM64 deployment preparation

This branch is a **staged upgrade**; do not deploy before validating end-to-end download and Telegram flows.

## Ubuntu 24.04 ARM64 prerequisites

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip python3-dev build-essential poppler-utils tesseract-ocr tesseract-ocr-hin tesseract-ocr-eng
mkdir -p ~/apps ~/data/yct-driver/{work,checkpoints} ~/.config/yct-driver
git clone -b feature/oracle-self-hosted https://github.com/akggautamasar/Yct.git ~/apps/Yct
cd ~/apps/Yct
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp deploy/oracle/env.example ~/.config/yct-driver/env
chmod 700 ~/.config/yct-driver
chmod 600 ~/.config/yct-driver/env
```

Edit `~/.config/yct-driver/env` with credentials **on the server only**.
Do not publish credentials, Telethon session files, or SSH keys.

## Service (only after testing)

```bash
sudo cp ~/apps/Yct/deploy/oracle/yct-driver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yct-driver
sudo systemctl status yct-driver --no-pager
curl -fsS http://127.0.0.1:8000/healthz
```

The service binds only to localhost. Use an SSH tunnel for private testing:

```bash
ssh -i ~/.ssh/yct.key -L 8000:127.0.0.1:8000 ubuntu@161.118.182.6
```

Then visit http://localhost:8000 on the same device. For public access later,
configure a TLS reverse proxy and strong authentication. Do not expose port 8000 directly.

## Storage and limits

The VM currently has approximately 44 GB free. `WORK_DIR` and the SQLite ledger
must be on persistent storage. Avoid uncontrolled parallel full-book assembly.
Telegram bot and user-session upload limits are distinct; verify before increasing
`TG_MAX_MB`. Larger `CHUNK_PAGES` does not bypass Telegram limits.

## Checkpoint migration

The local SQLite ledger starts empty. Keep `CHECKPOINT_GITHUB_TOKEN` configured
temporarily to consult previously uploaded parts. The new checkpoint backend writes
new entries locally and, if configured, also to GitHub. Removing the GitHub token
without importing old entries can result in duplicate uploads. Back up the SQLite
database before migrating or upgrading.
