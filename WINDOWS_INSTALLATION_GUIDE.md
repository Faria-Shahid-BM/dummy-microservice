# Windows Installation and Setup Guide

**API Gateway Platform — Local Development & Deployment on Windows**

| | |
|---|---|
| Document version | 1.0 |
| Operating system | Windows 10/11 |
| Deployment model | Docker Compose (single machine) |

This guide walks you through setting up the API Gateway Platform on a Windows workstation or server. Complete the sections in order.


---

## 1. Purpose and Scope

The platform runs as a Docker Compose stack on Windows using Docker Desktop. All services run in isolated Linux containers on a virtual network. One port (80) is published to your Windows host.

**In scope:** installation, verification, local development, and troubleshooting on Windows 10/11.

**Out of scope:** high availability, multi-host orchestration, or federation with external identity providers.

### 1.1 Solution Components

| Container | Image | Function | Published port |
|---|---|---|---|
| `frontend` | Built locally | Web client; forwards `/api/*` to gateway | 80 |
| `kong` | `kong:3` (Docker Hub) | API Gateway; token validation, entitlements | None |
| `auth-service` | Built locally | Authentication, token issuance, user admin | None |
| `document-reviewer` | Built locally | Document comparison | None |
| `collateral-reviewer` | Built locally | Collateral review; calls Azure OpenAI | None |
| `audit-service` | Built locally | Audit log retrieval | None |

Only port 80 is exposed to your Windows machine.

### 1.2 Gateway Routes

| Public path | Target service | Authentication |
|---|---|---|
| `/` and unmatched paths | Web client | None |
| `/api/auth/login` | `auth-service` | None; issues tokens |
| `/api/auth/users` | `auth-service` | Token + admin role |
| `/api/documents` | `document-reviewer` | Token + entitlement |
| `/api/collateral` | `collateral-reviewer` | Token + entitlement |
| `/api/audit` | `audit-service` | Token |

### 1.3 Persistent Data

Two directories hold all persistent data — these are the only files requiring backup:

| Host path | Container path | Contents |
|---|---|---|
| `.\auth-data\` | `/app/data` | `auth.db` — SQLite user store (accounts, hashes, entitlements) |
| `.\audit-logs\` | `/app/logs` | `audit.log` — append-only audit trail |

> These are host directories, not Docker volumes. `docker compose down -v` does not remove them.

---

## 2. Prerequisites

### 2.1 Hardware and OS

| Requirement | Specification |
|---|---|
| Operating system | Windows 10 (build 19041+) or Windows 11 |
| CPU | Intel or AMD processor with virtualization enabled (check in BIOS if VirtualBox/Hyper-V won't start) |
| Memory | Minimum 8 GB RAM; 16 GB recommended for comfortable development |
| Disk space | 40 GB free space (for Docker images, containers, and data) |
| Network | Outbound HTTPS to Docker Hub, npm registry, Python Package Index, and the LLM API endpoint (if configured) |

### 2.2 Required Software

| Software | Version | Where to get | Verify with |
|---|---|---|---|
| Docker Desktop | 4.10 or later | https://www.docker.com/products/docker-desktop | `docker --version` |
| Git for Windows | Any current release | https://git-scm.com/download/win | `git --version` |
| PowerShell | 5.0+ (built-in on Windows 10/11) | Built-in | `$PSVersionTable.PSVersion` |

> Docker Desktop includes Docker Engine and Docker Compose. Installation on Windows requires Hyper-V or WSL2; see Section 3.

### 2.3 Configuration Prerequisites

| Item | Note |
|---|---|
| Deployment package | `.env` file and `keys/` directory (pre-configured per deployment) |
| LLM API credentials | Required only if using collateral-reviewer service; see `.env.example` |
| DNS hostname | For production; not required for local development |

---

## 3. Pre-Installation Checklist

- [ ] Windows 10/11 build number verified (see note below)
- [ ] Virtualization enabled in BIOS/UEFI (VirtualBox or Hyper-V capable)
- [ ] No other Hyper-V services running (e.g., VirtualBox in bridging mode)
- [ ] 40+ GB free disk space
- [ ] Administrator access to install Docker
- [ ] Deployment package received (`.env` and `keys/` directory)
- [ ] LLM API credentials available (if using collateral-reviewer)

**Check Windows build:**
```powershell
[System.Environment]::OSVersion.Version
# Or: Settings > System > About > Windows specifications > OS build
```

Requires build 19041 or later for WSL2 support.

**Check if virtualization is enabled:**
```powershell
# Open PowerShell as Administrator and run:
Get-WindowsFeature Hyper-V | Select DisplayName, InstallState
# Should show Installed=True
```

If Hyper-V is not installed, use Windows search to run "Turn Windows features on or off" and check the Hyper-V box.

---

## 4. Installation

### 4.1 Install Docker Desktop

1. **Download Docker Desktop:** https://www.docker.com/products/docker-desktop
2. **Run the installer** and follow the default settings. Docker will install Hyper-V and WSL2 if not already present — a restart may be required.
3. **Verify installation:** Open PowerShell and run:
   ```powershell
   docker --version
   docker compose version
   docker run hello-world
   ```

All three commands should succeed without errors.

> **Note on WSL2:** Docker Desktop on Windows uses either Hyper-V or WSL2 as the virtualization backend. WSL2 is now the default and recommended. If prompted, accept the WSL2 installation.

### 4.2 Deploy the Application Package

#### Option A: Clone from Git

```powershell
# Choose a location to store the project; C:\dev\api-gateway is suggested
cd C:\dev
git clone [repository-url] api-gateway
cd .\api-gateway
```

#### Option B: Extract from an Archive

```powershell
# If you received a .zip or .tar.gz file:
cd C:\dev
# Extract here (use 7-Zip, WinRAR, or Windows built-in extraction)
# Then verify the contents:
cd .\api-gateway
```

#### Verify Contents

```powershell
# Check that all required files exist:
ls
# Should show:
#   docker-compose.yml   kong.yml   .env   requirements.txt
#   keys\  common\  kong-plugins\  frontend\
#   auth-service\  audit-service\  document-reviewer\  collateral-reviewer\
```

> **Important:** If `.env` or `keys\` is missing, the package is incomplete. Do not proceed; request them from your administrator. These cannot be recreated locally.

All subsequent commands assume you are in the `api-gateway` directory.

### 4.3 Verify the Environment Configuration

The `.env` file contains LLM API configuration for the collateral-reviewer service. Check `.env.example` to see required variables:

```powershell
# Check that .env exists and is not empty:
Get-Content .env | Measure-Object -Line
# Should show a non-zero count

# See what variables are required:
Get-Content .env.example
```

Required variables (if using collateral-reviewer):
- `LLM_BASE_URL` — Base URL of your LLM API (e.g., `https://api.openai.com/v1`)
- `LLM_API_KEY` — Your API key
- `LLM_MODEL_EXTRACTION` — Model for extraction (default: `gpt-4o-mini`)
- `LLM_MODEL_VISION` — Model for vision/OCR (default: `gpt-4o-mini`)

> **Security:** Treat `.env` as sensitive — it contains API credentials. Never commit it to version control. Do not edit the values unless instructed by your administrator.

### 4.4 Verify the Token Signing Keys

Access tokens are signed with RS256. The key pair is supplied in `keys/`.

```powershell
# Check that both keys exist:
ls .\keys\
# Should show: jwt-private.pem and jwt-public.pem

# Verify the public key is in kong.yml:
Select-String -Path .\kong.yml -Pattern "rsa_public_key" -Context 2
# Should display a public key block (BEGIN PUBLIC KEY ... END PUBLIC KEY)
```

> **Security:** `keys/jwt-private.pem` is confidential. Keep it secure and include it in backups. `keys/jwt-public.pem` is not confidential and is already in `kong.yml`.

### 4.5 Configure Windows Firewall

Port 80 must be reachable. Docker Desktop typically handles this automatically, but confirm:

```powershell
# Run as Administrator:
New-NetFirewallRule -DisplayName "API Gateway Port 80" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow
```

Verify:
```powershell
Get-NetFirewallRule -DisplayName "API Gateway Port 80"
```

> **For WSL2:** If using WSL2 backend, Docker Desktop automatically manages the firewall. This step is optional but safe to run.

### 4.6 Build the Container Images

```powershell
cd C:\dev\api-gateway
docker compose build
```

**Expected output:** Each service builds in sequence. Initial builds take 2–5 minutes and require downloads from Docker Hub and the npm/Python registries.

**Troubleshooting slow builds:**
- Check your internet connection
- Run `docker system prune -a` to remove unused images (frees ~5 GB)
- If a build fails, run `docker compose build --no-cache` to rebuild without using cached layers

### 4.7 Start the Platform

```powershell
cd C:\dev\api-gateway
docker compose up -d
```

The `-d` flag runs containers in the background. To see startup logs:

```powershell
docker compose logs --tail=50 -f
# Press Ctrl+C to stop following logs
```

Expected sequence:
1. `kong` starts first and reports "healthy" after ~10 seconds
2. Other services start once kong is healthy
3. Each service reports "Application startup complete" in its logs

---

## 5. Verification

### 5.1 Check Container Status

```powershell
docker compose ps
```

Expected output:

```
NAME                   STATUS              PORTS
api_gateway-kong              running (healthy)   
api_gateway-auth-service      running             
api_gateway-document-reviewer running             
api_gateway-collateral-reviewer running           
api_gateway-audit-service     running             
api_gateway-frontend          running             0.0.0.0:80->80/tcp
```

All containers show `running`. The `kong` container additionally shows `healthy`.

### 5.2 Test the Web Client

Open your browser and visit:
- **Local machine:** `http://localhost/`
- **Another machine on the network:** `http://[your-windows-hostname]/` (e.g., `http://MYPC/`)

You should see a sign-in page.

> **To find your Windows hostname:**
> ```powershell
> [System.Net.Dns]::GetHostName()
> ```

### 5.3 Test Authentication

```powershell
# Obtain a token using the test account:
$response = Invoke-WebRequest -Uri "http://localhost/api/auth/login" `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"username":"alice","password":"alicepw"}'

$response.Content | ConvertFrom-Json | Format-List
```

Expected response includes:
```json
{
  "access_token": "eyJ0eXAi...",
  "username": "alice",
  "role": "admin",
  "services": ["documents", "collateral"]
}
```

### 5.4 Review Logs

```powershell
# Show the last 50 lines from all services:
docker compose logs --tail=50

# Follow logs in real-time (Ctrl+C to stop):
docker compose logs -f

# View logs from one service:
docker compose logs --tail=100 auth-service
```

---

## 6. Post-Installation Setup

### 6.1 Replace Demo Accounts

Two test accounts are created during startup for verification:

| Username | Password | Role |
|---|---|---|
| `alice` | `alicepw` | Administrator |
| `bob` | `bobpw` | Viewer |

**Before production use, you MUST delete these accounts:**

1. Sign in to the web client with `alice` / `alicepw`
2. Create a real administrator account using the Users page or via API:
   ```powershell
   # First, get a token:
   $loginResponse = Invoke-WebRequest -Uri "http://localhost/api/auth/login" `
     -Method POST `
     -ContentType "application/json" `
     -Body '{"username":"alice","password":"alicepw"}'
   $token = ($loginResponse.Content | ConvertFrom-Json).access_token
   
   # Then create the admin user:
   Invoke-WebRequest -Uri "http://localhost/api/auth/users" `
     -Method PUT `
     -Headers @{ Authorization = "Bearer $token" } `
     -ContentType "application/json" `
     -Body '{"username":"admin","password":"SecurePassword123","role":"admin","services":["documents","collateral"]}'
   ```
3. Verify the new account works
4. Delete `alice` and `bob` using the Users page

> **Security requirement:** Demo accounts must be removed before the system handles production data. If you redeploy or restore a backup, confirm they are gone.

### 6.2 Backup Your Data

Back up these directories regularly:

```powershell
# Example: create a backup to a USB drive or network location
$timestamp = Get-Date -Format "yyyy-MM-dd-HHmmss"
$backupDir = "E:\Backups\api-gateway"  # Change to your backup location

# Create backup directory if it doesn't exist
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

# Compress auth-data and audit-logs
Compress-Archive -Path ".\auth-data", ".\audit-logs" `
  -DestinationPath "$backupDir\api-gateway-$timestamp.zip" `
  -Force

# Also back up the keys and .env (treat as sensitive)
Copy-Item ".\keys\*" "$backupDir\keys-$timestamp\" -Recurse -Force
Copy-Item ".\.env" "$backupDir\.env-$timestamp" -Force

Write-Host "Backup complete: $backupDir\api-gateway-$timestamp.zip"
```

**Recommended backup schedule:** Daily or before any significant change.

---

## 7. Common Tasks

### 7.1 Stop the Platform (Keeping Data)

```powershell
docker compose stop
```

Containers stop, but data in `auth-data\` and `audit-logs\` is preserved. Restart with `docker compose up -d`.

### 7.2 Start a Stopped Platform

```powershell
docker compose up -d
```

### 7.3 View Real-Time Logs

```powershell
docker compose logs -f [service]
```

Example:
```powershell
docker compose logs -f auth-service
docker compose logs -f collateral-reviewer
```

### 7.4 Rebuild After Code Changes

If you modify Python files (e.g., in `auth-service/`):

```powershell
docker compose build [service]
docker compose up -d [service]
```

Example:
```powershell
docker compose build auth-service
docker compose up -d auth-service
```

### 7.5 Open a Shell in a Running Container

For debugging:

```powershell
docker compose exec [service] sh
```

Example:
```powershell
docker compose exec auth-service sh
# Then run commands like: ls -la, cat /app/config.py, etc.
# Type 'exit' to quit
```

### 7.6 Clean Up Docker Disk Space

Docker images and containers can consume significant disk space over time.

```powershell
# Remove stopped containers and unused images:
docker system prune -a

# Remove unused volumes (be careful — this cannot be undone):
docker volume prune
```

### 7.7 Update to a New Version

```powershell
# 1. Back up your data (see Section 6.2)

# 2. Pull the latest code
git pull

# 3. Verify configuration is intact
ls .\keys\jwt-private.pem
Select-String -Path .\kong.yml -Pattern "rsa_public_key"
ls .\.env

# 4. Rebuild and restart
docker compose build
docker compose up -d

# 5. Re-run Section 5 to verify
docker compose ps
```

---

## 8. Troubleshooting

| Symptom | Probable cause | Solution |
|---|---|---|
| `docker: command not found` | Docker not installed or not in PATH | Restart PowerShell or your IDE after Docker Desktop install. Reinstall if needed. |
| Containers show `exited` status | Startup error, often missing `.env` | Run `docker compose logs [service]`. Confirm `.env` exists and has content. |
| `docker compose: variable is not set` | `.env` file missing or incomplete | Verify `.env` exists in the project directory. Check `.env.example` for required variables. |
| `ports are already allocated` | Port 80 in use by another service | Run `netstat -ano \| findstr :80` to identify the conflicting process. Kill it or change Docker to use port 8080. |
| `auth-service` fails to start | Missing `keys/jwt-private.pem` | Verify the file exists. If missing, request from your administrator. |
| Authentication works, but API requests return 401 | Public key mismatch in `kong.yml` | Verify the `rsa_public_key` block matches `keys/jwt-public.pem`. If it shows a placeholder, replace it with the contents of `jwt-public.pem` and run `docker compose up -d --force-recreate`. |
| Browser shows "Connection refused" | Firewall blocking port 80 or containers not running | Run `docker compose ps` to check status. Run `Test-NetConnection localhost -Port 80` to test connectivity. |
| Collateral review fails | LLM API unreachable or invalid credentials | Check `docker compose logs collateral-reviewer`. Verify `.env` has `LLM_BASE_URL` and `LLM_API_KEY` set correctly. Confirm outbound HTTPS is allowed to your LLM endpoint. |
| Large file uploads fail | nginx limit exceeded | Edit `frontend/nginx.conf`, increase `client_max_body_size`, rebuild: `docker compose build frontend && docker compose up -d`. |
| High disk usage | Old Docker images consuming space | Run `docker system prune -a` to clean up unused images. |
| Cannot access from another machine | Windows Firewall blocking inbound port 80 | Run the firewall rule from Section 4.5. Or try accessing via `http://[your-hostname]/` instead of `localhost`. |

---

## 9. Architecture Overview

```
Your Windows Machine
├─ Docker Desktop
│  ├─ Hyper-V or WSL2 (virtual Linux kernel)
│  └─ Docker Containers (all Linux)
│     ├─ frontend (nginx) ──┐
│     ├─ kong (gateway)     │── on internal bridge network 172.19.0.0/16
│     ├─ auth-service       │
│     ├─ document-reviewer  │
│     ├─ collateral-reviewer │
│     └─ audit-service ─────┘
│
└─ Published to Windows Host
   └─ Port 80 → frontend container port 80
```

**Key point:** Docker Desktop runs a lightweight Linux VM. All services are Linux containers. Windows sees only port 80 as exposed; internal traffic (between containers) is isolated and invisible to Windows.

---

## 10. Reference: Common Commands

| Command | Purpose |
|---|---|
| `docker compose up -d` | Start all services in background |
| `docker compose down` | Stop and remove containers (keeps data) |
| `docker compose ps` | Show status of all containers |
| `docker compose logs -f [service]` | Follow logs in real-time |
| `docker compose build [service]` | Rebuild one service after code changes |
| `docker compose exec [service] sh` | Open a shell in a running container |
| `docker system prune -a` | Clean up unused images and containers |

---

## 11. Getting Help

| Issue | Action |
|---|---|
| Container won't start | Run `docker compose logs [service]` and search error messages for clues |
| Lost or corrupted data | Restore from your backup (Section 6.2) |
| Docker Desktop won't start | Check Event Viewer > Windows Logs > System for Hyper-V errors. Restart your machine. |
| Need to reset everything | Stop Docker, run `docker system prune -a`, then delete `auth-data\` and `audit-logs\` directories (data loss!), then restart. |


