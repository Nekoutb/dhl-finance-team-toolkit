# Deploying the Finance Team Toolkit to www.rtrotc.com

This is a **Python (FastAPI)** app. It needs a **Hostinger VPS** (Ubuntu), not
shared hosting — shared/Premium/Business plans run PHP only and cannot run a
Python/uvicorn process. Your domain `www.rtrotc.com` is registered with
Hostinger; we point its DNS at the VPS.

Roughly 30 minutes end to end.

---

## 0. Prerequisites
- A **Hostinger VPS** (KVM 1 is plenty) running **Ubuntu 22.04/24.04**. Note
  its **public IP** (hPanel → VPS → Overview).
- SSH access (hPanel shows the root password, or add your SSH key).

## 1. Point the domain at the VPS (Hostinger hPanel)
hPanel → Domains → `rtrotc.com` → **DNS / Nameservers → DNS records**:

| Type | Name | Value | TTL |
|------|------|-------|-----|
| A    | `@`  | `<VPS_PUBLIC_IP>` | 3600 |
| A    | `www`| `<VPS_PUBLIC_IP>` | 3600 |

Delete any conflicting A/AAAA/CNAME on `@` and `www` that point to shared
hosting. DNS takes 5–60 min to propagate.

## 2. Server setup (SSH in as root)
```bash
apt update && apt install -y python3 python3-venv python3-pip nginx git
adduser --system --group www-data 2>/dev/null || true

# Get the code (private repo — use a GitHub Personal Access Token or deploy key)
git clone https://github.com/Nekoutb/dhl-finance-team-toolkit.git /opt/finance-toolkit
cd /opt/finance-toolkit

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

mkdir -p data && chown -R www-data:www-data /opt/finance-toolkit
```

## 3. Create the staff login (enables auth)
```bash
.venv/bin/python scripts/set_password.py <username> '<strong-password>'
# repeat for each staff member; auth is now ON with HTTPS-secure cookies
```
This writes `config.json` (git-ignored — stays on the server only). Configure
SMTP and other settings afterwards from the in-app ⚙️ Settings page, or edit
`config.json` directly.

## 4. Run the app as a service
```bash
cp deploy/finance-toolkit.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now finance-toolkit
systemctl status finance-toolkit            # should be active (running)
curl -s localhost:8801/healthz              # {"status":"ok",...}
```

## 5. Nginx + HTTPS
```bash
cp deploy/nginx-rtrotc.conf /etc/nginx/sites-available/rtrotc.com
ln -s /etc/nginx/sites-available/rtrotc.com /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# Free TLS certificate (auto-renewing) once DNS resolves to this server:
apt install -y certbot python3-nginx
certbot --nginx -d rtrotc.com -d www.rtrotc.com --redirect -m <you@email> --agree-tos -n
```
Certbot adds the 443 block + forces HTTP→HTTPS. Because `set_password.py` set
`secure_cookies = true`, the session cookie is only sent over HTTPS.

## 6. Verify
- https://www.rtrotc.com → redirected to the **Sign in** page.
- Log in → the dashboard. Customer remittance links (`/portal/<token>`) and
  file downloads remain reachable without login by design.

## Updating later
```bash
cd /opt/finance-toolkit && git pull && .venv/bin/pip install -r requirements.txt
systemctl restart finance-toolkit
```

## Notes
- `config.json` and `data/` are git-ignored — real credentials and finance
  data live only on the VPS, never in the repo.
- Back up `/opt/finance-toolkit/data/` and `config.json` (cron + offsite).
- The `.bat` launchers and port-8801 notes are for local Windows use; on the
  VPS the systemd service owns the process.
