# Ubuntu Deployment And Update Tutorial

This file documents the exact workflow to push local edits and deploy them on the Ubuntu server.

## 1. Local Development Workflow (Windows)

From your local repository:

```powershell
cd "d:\Programing\New folder (4)\study_platform"
git add -A
git commit -m "describe your change"
git push origin main
```

## 2. Pull And Deploy On Ubuntu Server

SSH to Ubuntu, then run:

```bash
cd /var/www/edupath
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart edupath
sudo systemctl status edupath
```

## 3. Reload Nginx (only when Nginx config changes)

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 4. Quick Health Checks

Run on Ubuntu:

```bash
curl -I http://127.0.0.1:8000
curl -I http://127.0.0.1
curl -I https://edu-path.app
```

Expected:
- Gunicorn responds on `127.0.0.1:8000`
- Nginx responds on `127.0.0.1`
- Public domain responds with valid HTTPS

## 5. Service Logs (when something breaks)

```bash
sudo systemctl status edupath
sudo journalctl -u edupath -n 100 --no-pager
sudo tail -n 100 /var/log/nginx/error.log
```

## 6. Current systemd Service Reference

`/etc/systemd/system/edupath.service` should use:

```ini
[Unit]
Description=EduPath Gunicorn Service
After=network.target

[Service]
User=edupathadmin
Group=www-data
WorkingDirectory=/var/www/edupath
EnvironmentFile=/var/www/edupath/.env
ExecStart=/var/www/edupath/.venv/bin/gunicorn --workers 3 --bind 127.0.0.1:8000 wsgi:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

After editing service file:

```bash
sudo systemctl daemon-reload
sudo systemctl restart edupath
```

## 7. Optional: One-command deploy script

Create script:

```bash
cat > /var/www/edupath/deploy.sh << 'EOF'
#!/usr/bin/env bash
set -e
cd /var/www/edupath
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart edupath
sudo systemctl status --no-pager edupath
EOF
chmod +x /var/www/edupath/deploy.sh
```

Then deploy with:

```bash
/var/www/edupath/deploy.sh
```

## 8. Security Notes

- Keep `.env` server-only and permission-limited (`chmod 600`).
- Rotate exposed credentials immediately (MongoDB, Redis, SECRET_KEY).
- Keep Redis and MongoDB private-only when possible.
- Keep regular backups for MongoDB.
