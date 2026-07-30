# Deployment and Installation Guide

**CAD Workbench — Customer-Managed Server Environment**

| | |
|---|---|
| Document version | 1.0 |
| Deployment model | Single-server Docker Compose |

This document describes how to install and verify the API Gateway Platform on a customer-managed Linux
server. Complete the sections in order.

Values in square brackets, such as `[app.clientdomain.com]`, are environment-specific and must be
replaced before the command is run. Use all other values exactly as shown.

The deployment package includes the environment configuration and the token signing keys, both
pre-configured by `[Vendor]`.

---

## 1. Purpose and Scope

The platform is installed as a single Docker Compose deployment on one Linux server. The gateway, the
application services, and the web client run on an isolated internal network. One port is published
externally.

**In scope:** installation, verification, updates, and troubleshooting for a single-server deployment.

**Out of scope:** high availability and multi-host orchestration, federation with an external identity
provider, and migration of the user store to a managed database service. See Section 10.

### 1.1 Solution Components

| Container | Image | Function | Published port |
|---|---|---|---|
| `frontend` | Built locally | Serves the web client and forwards `/api/*` requests to the gateway | 80 |
| `kong` | `kong:3` (Docker Hub) | Gateway; validates access tokens and enforces service entitlements | None |
| `auth-service` | Built locally | Authentication, token issuance, and user administration | None |
| `document-reviewer` | Built locally | Document comparison | None |
| `collateral-reviewer` | Built locally | Collateral document review; calls the Azure OpenAI endpoint | None |
| `audit-service` | Built locally | Audit log retrieval | None |

Port 80 on the `frontend` container is the only port published outside the server.

> **Image builds.** Application images are built on the target server. Use `docker compose build` to
> build and update them. The `docker compose pull` command does not retrieve application images.

> **Database.** No database server is required. Gateway configuration is read from `kong.yml`, and
> persistent data is limited to the files listed in Section 1.3.

### 1.2 Gateway Routes

| Public path | Target service | Authentication |
|---|---|---|
| `/` and all unmatched paths | Web client (static content) | None |
| `/api/auth/login` | `auth-service` | None; issues access tokens |
| `/api/auth/users` | `auth-service` | Token; administrator role |
| `/api/documents` | `document-reviewer` | Token and service entitlement |
| `/api/collateral` | `collateral-reviewer` | Token and service entitlement |
| `/api/audit` | `audit-service` | Token |

### 1.3 Persistent Data

Two host directories are mounted into containers. These are the only data requiring backup.

| Host path | Container path | Contents |
|---|---|---|
| `./auth-data/` | `/app/data` | `auth.db` — user store containing accounts, password hashes, and service entitlements |
| `./audit-logs/` | `/app/logs` | `audit.log` — append-only audit trail |

> These are host directories, not Docker volumes. `docker compose down -v` does not remove them.



---

## 2. Prerequisites

### 2.1 Server and Operating System

| Requirement | Specification |
|---|---|
| Operating system | 64-bit Linux. Commands below assume Ubuntu 22.04 LTS; adjust package management commands for other distributions |
| CPU, memory, storage | Minimum 4 vCPU, 8 GB RAM, 40 GB disk |
| Outbound network access | Docker Hub (`kong:3`, `node:20-alpine`, `nginx:1.27-alpine`), the Python Package Index, the npm registry, and the Azure OpenAI endpoint |
| Inbound network access | TCP port 80 from user workstations. No other inbound port is required |
| Server access | SSH access using an account in the `sudo` and `docker` groups |
| Name resolution | A DNS hostname resolving to the server, such as `[app.clientdomain.com]` |

> Outbound access to the package registries is required while images are built (Section 4.6). For
> servers without internet access, use the procedure in Section 4.6.1.

> The Azure OpenAI endpoint must be reachable from this server. Confirm the outbound firewall permits it
> before installation.

### 2.2 Required Software

| Software | Version | Verification command |
|---|---|---|
| Docker Engine | 24.x or later | `docker --version` |
| Docker Compose plugin | v2.x | `docker compose version` |
| git | Any current release | `git --version` — required only when the application package is retrieved from a repository |

### 2.3 Prerequisite Information

| Item | Description |
|---|---|
| External hostname | Such as `[app.clientdomain.com]` |
| TLS certificate and private key | Required only where TLS terminates on this server; see Section 2.4 |
| Firewall change procedure | Required to open TCP port 80, and TCP port 443 where TLS terminates on this server |

> The Azure OpenAI configuration and the token signing keys are supplied with the deployment package.

### 2.4 TLS Termination

The platform listens on HTTP only. Agree the termination point with the customer's network team before
installation.

**Termination on upstream infrastructure.** A load balancer, reverse proxy, or web application firewall
in front of the server holds the certificate and forwards HTTP traffic to port 80. No application change
is required.

**Termination on this server.** Requires changes outside the scope of this procedure: a `443` server
block in `frontend/nginx.conf`, the certificate and key mounted into the `frontend` container, port 443
published in `docker-compose.yml` and opened on the host firewall, and a redirect from port 80.

---

## 3. Pre-Installation Checklist

| Item | Confirmed |
|---|---|
| Server provisioned and accessible over SSH | ☐ |
| Docker Engine 24.x or later and Docker Compose v2 installed (Section 4.1) | ☐ |
| Deployment package received, including the `.env` file and the `keys/` directory | ☐ |
| Outbound access to Docker Hub, the Python Package Index, and the npm registry confirmed | ☐ |
| Outbound access to the Azure OpenAI endpoint confirmed (Section 2.1) | ☐ |
| TCP port 80 approved by the network and security teams | ☐ |
| TLS termination point agreed (Section 2.4) | ☐ |
| TLS certificate available, where applicable | ☐ |
| Data processing confirmed with security and compliance (Section 1.4) | ☐ |
| Maintenance window scheduled, where this installation updates a running system | ☐ |

---

## 4. Installation

### 4.1 Install Docker Engine and Docker Compose

Skip this section if Docker is already installed at the required version.

```bash
# Update package index and install prerequisites
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg

# Add Docker's official GPG key and repository
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

# Install Docker Engine, CLI and the Compose plugin
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

# Allow the current user to run docker without sudo (log out and back in after this)
sudo usermod -aG docker $USER
```

Verify:

```bash
docker --version          # expect 24.x or later
docker compose version    # expect v2.x
docker run hello-world    # expect "Hello from Docker!"
```

> On Red Hat Enterprise Linux, Amazon Linux, and comparable distributions, use the equivalent `yum` or
> `dnf` procedure from the Docker documentation instead of the `apt-get` commands above.

### 4.2 Deploy the Application Package

```bash
sudo mkdir -p /opt/api-gateway
sudo chown $USER:$USER /opt/api-gateway

# Either clone from the repository:
git clone [repository-url] /opt/api-gateway

# Or extract a supplied archive:
scp deployment-package.tar.gz [user]@[server-host]:/opt/
cd /opt && tar -xzf deployment-package.tar.gz -C /opt/api-gateway --strip-components=1
```

Confirm the expected contents:

```bash
cd /opt/api-gateway
ls -la
# Expect at minimum:
#   docker-compose.yml   kong.yml   .env   requirements.txt
#   keys/  common/  kong-plugins/  frontend/
#   auth-service/  audit-service/  document-reviewer/  collateral-reviewer/
```

Run all subsequent commands from `/opt/api-gateway`.

> If either `.env` or `keys/` is absent, the package is incomplete. Do not proceed; contact `[Vendor]`.
> These items are supplied per deployment and cannot be recreated on site.

### 4.3 Verify the Environment Configuration

The supplied `.env` file contains the Azure OpenAI configuration used by `collateral-reviewer`. No values
need to be entered.

```bash
cd /opt/api-gateway
chmod 600 .env
grep -c . .env    # expect a non-zero count
```

> Treat `.env` as confidential; it contains an API credential. Do not store it in version control. Do
> not edit the values — configuration changes are issued by `[Vendor]` as a replacement file.

### 4.4 Verify the Token Signing Keys

Access tokens are signed using RS256. The key pair is generated per deployment and supplied in `keys/`,
with the public key already present in `kong.yml`.

```bash
cd /opt/api-gateway
ls -l keys/                        # expect jwt-private.pem and jwt-public.pem
chmod 600 keys/jwt-private.pem
grep -A2 rsa_public_key kong.yml   # expect a BEGIN PUBLIC KEY block
```

If `keys/` is empty, or the third command returns `REPLACE_BY_RUNNING_...` instead of a key, the package
is incomplete. Do not start the platform; contact `[Vendor]`.

`keys/jwt-private.pem` is confidential. Keep it at mode `600`, exclude it from version control, and
include it in the backup procedure in Section 6.1. `keys/jwt-public.pem` is not confidential.

> Do not modify the `rsa_public_key` block in `kong.yml`; it must correspond to the supplied private key.

> **Key rotation.** Rotation is performed by `[Vendor]` and issued as a replacement key pair. It
> invalidates all issued tokens and requires users to sign in again, so apply it within a maintenance
> window. Request rotation immediately if the private key is suspected of being disclosed.

### 4.5 Configure the Host Firewall

TCP port 80 is the only port requiring external reachability.

```bash
sudo ufw allow 80/tcp
sudo ufw status
```

> **Security requirement.** Do not open any other application port. The gateway Administration API on
> port 8001 is unauthenticated, is reachable only on the internal network, and must remain inaccessible
> from outside the server.

### 4.6 Build the Container Images

```bash
cd /opt/api-gateway
docker compose build
```

Initial builds require several minutes.

#### 4.6.1 Deployment Without Internet Access

Build the images on a connected system, export them, and load them onto the target server:

```bash
# On a machine with internet access, in a copy of this project:
docker compose build
docker save -o app-images.tar \
  api_gateway-frontend api_gateway-auth-service api_gateway-audit-service \
  api_gateway-document-reviewer api_gateway-collateral-reviewer \
  kong:3

# Transfer app-images.tar to the server, then:
docker load -i app-images.tar
```

> `.env` and `keys/` are read from the target server at startup and are not included in the images.
> Complete Sections 4.3 and 4.4 on the target server after loading the images.

### 4.7 Start the Platform

```bash
cd /opt/api-gateway
docker compose up -d
```

The gateway starts first; the application services start once it reports a healthy state.

> Containers do not restart after a host reboot unless a restart policy is configured. To enable it, add
> `restart: unless-stopped` to each service definition in `docker-compose.yml` and re-run
> `docker compose up -d`.

---

## 5. Verification

### 5.1 Verify Container Status

```bash
docker compose ps
```

All containers report `running`. The `kong` container additionally reports `healthy`. No health check is
defined for the remaining services, so they report no health status.

### 5.2 Review Service Logs

```bash
docker compose logs --tail=100
# Or for one service:
docker compose logs --tail=100 -f kong
docker compose logs --tail=100 -f auth-service
```

A successful start produces `Application startup complete.` from each application service and
`declarative config loaded from /kong.yml` from the gateway.

### 5.3 Verify Application Response

Run the following on the server:

```bash
# Confirm the web client responds — expect HTTP 200
curl -I http://localhost/

# Authenticate and obtain a token (see Section 5.4 regarding this account)
curl -s http://localhost/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"alice","password":"alicepw"}'
```

A successful response returns a token together with the account's username, role, and entitled services.

From a workstation outside the server, open `http://[app.clientdomain.com]/` in a browser. Confirm the
sign-in page loads, that authentication succeeds, and that only the services the account is entitled to
are presented.

### 5.4 Replace the Demonstration Accounts

Two accounts are created automatically when the user store is initialised, to verify authentication
during installation.

| Username | Password | Role |
|---|---|---|
| `alice` | `alicepw` | Administrator |
| `bob` | `bobpw` | Viewer |

Entitlements for these accounts are preset for verification only. Assign entitlements explicitly when
creating production accounts.

> **Security requirement.** Both demonstration accounts must be deleted before the platform enters
> production use:
>
> 1. Use `alice` to complete the verification in Section 5.3.
> 2. Create the production administrator account using the administration interface or
>    `PUT /api/auth/users`, and confirm authentication succeeds with it.
> 3. Delete `alice` and `bob`, and record completion in Appendix A.
>
> These accounts are recreated if the user store is reinitialised. Confirm their absence after any
> restore or redeployment.

---

## 6. Operations

### 6.1 Backup

Back up the two host directories listed in Section 1.3. No database dump is required.

```bash
# Example: nightly copy to a backup location
tar -czf /backup/api-gateway-$(date +%F).tar.gz \
  -C /opt/api-gateway auth-data audit-logs
```

`auth-data/auth.db` is a SQLite database. Obtain a consistent copy either with the platform stopped
(`docker compose stop`) or by running `sqlite3 auth.db ".backup"` against the running database.

Back up `keys/jwt-private.pem` and `.env` separately, using a procedure appropriate to confidential
material. Both are supplied per deployment and cannot be recreated on site.

> **Audit retention.** In regulated environments, forward `audit-logs/audit.log` to the customer's
> security information and event management platform or to write-once storage. A file on a single server
> does not satisfy durable audit retention requirements.

### 6.2 Updating the Platform

Apply updates by rebuilding the application images.

```bash
cd /opt/api-gateway

# 1. Back up first (§6.1)
tar -czf /backup/api-gateway-pre-update-$(date +%F).tar.gz -C /opt/api-gateway auth-data audit-logs

# 2. Get the new code
git pull            # or extract the new archive over the directory

# 3. Confirm configuration survived the update
ls -l keys/jwt-private.pem                         # must still be present
grep -A2 rsa_public_key kong.yml                   # must be a real key, NOT the placeholder
ls -la .env                                        # confirm .env still present

# 4. Rebuild and restart
docker compose build
docker compose up -d
```

Only containers whose images changed are recreated. Repeat Section 5 after every update.

> **Configuration after an update.** An update can overwrite the public key in `kong.yml` with the
> placeholder value. The symptom is successful authentication followed by HTTP 401 responses to all
> subsequent API requests. To restore service, replace the `rsa_public_key` block in `kong.yml` with the
> contents of `keys/jwt-public.pem`, then run `docker compose up -d --force-recreate`. Step 3 detects
> this before it affects users.

### 6.3 Stopping and Removing the Deployment

```bash
# Stop, keeping containers and data
docker compose stop

# Remove containers and the network, keeping images
docker compose down
```

> **Data removal.** `docker compose down -v` removes named volumes. This deployment defines none, and
> `auth-data/` and `audit-logs/` are unaffected. Deleting application data requires removing those
> directories manually, which destroys all user accounts and the entire audit trail. Confirm a current
> backup exists first.

---

## 7. Troubleshooting

| Symptom | Probable cause | Resolution |
|---|---|---|
| A service reports `restarting` or `exited` in `docker compose ps` | Startup error, most often a missing `.env` value | Review `docker compose logs [service]`; confirm `.env` is present and populated (Section 4.3) |
| `docker compose up` reports `variable is not set` | `.env` missing or incomplete | Confirm `.env` is present (Section 4.3). If absent, request a replacement from `[Vendor]` |
| `auth-service` does not start; logs report `cannot read the JWT signing key` | `keys/jwt-private.pem` missing or not readable | Confirm the file is present and has mode `600` (Section 4.4), then run `docker compose up -d` |
| Authentication succeeds, but all subsequent API requests return HTTP 401 | The public key in `kong.yml` does not match the supplied private key | Run `grep -A2 rsa_public_key kong.yml`. If the placeholder or a superseded key is present, replace the block with the contents of `keys/jwt-public.pem`, then run `docker compose up -d --force-recreate` |
| Authentication requests return HTTP 500 | RS256 support unavailable in the `auth-service` image | Confirm `requirements.txt` specifies `pyjwt[crypto]`, then run `docker compose build auth-service` |
| An API request returns HTTP 403 with a valid token | Expected behaviour; the account is not entitled to that service | Grant the entitlement using the administration interface or `PUT /api/auth/users/[username]/services` |
| The platform is unreachable from a browser | Port 80 closed on the host firewall, or another process is bound to port 80 | Run `curl -I http://localhost/` on the server. If it succeeds, review the firewall configuration (Section 4.5). If it fails, identify the conflicting process with `sudo ss -ltnp \| grep :80` |
| The web client loads but API requests fail | The gateway is not healthy, or a service is unavailable | Confirm `kong` reports `healthy` in `docker compose ps`, then review `docker compose logs kong` |
| Collateral review fails while other services operate normally | The Azure OpenAI endpoint is unreachable from this server, or the supplied credential has expired | Review `docker compose logs collateral-reviewer`. Confirm the outbound firewall permits the endpoint (Section 2.1). If the endpoint is reachable, report the failure to `[Vendor]` |
| Document upload fails for large files | The upload exceeds the 50 MB limit configured in nginx | Increase `client_max_body_size` in `frontend/nginx.conf`, then run `docker compose build frontend && docker compose up -d` |
| Changes to `.env` have no effect | Containers were not recreated | Run `docker compose up -d` |
| Changes to the web client are not reflected | The web client requires an image rebuild | Run `docker compose build frontend && docker compose up -d` |
| Containers absent after a host reboot | No restart policy configured | Apply the note in Section 4.7 |

---

## 8. Appendix A — Deployment Record

Store this record securely and do not transmit it in plain text.

| Item | Value |
|---|---|
| Server hostname or IP address | |
| External application URL | |
| Published port | 80 |
| Installation directory | `/opt/api-gateway` |
| Azure OpenAI resource endpoint | |
| Azure OpenAI deployment names (extraction, vision) | |
| Azure region hosting the resource | |
| Data processing confirmed (Section 1.4) | ☐ |
| Token signing key pair received (Section 4.4) | ☐ Included in secure backup |
| `.env` file received (Section 4.3) | ☐ Included in secure backup |
| TLS termination point | |
| Backup destination | |
| Production administrator account | |
| Demonstration accounts removed (Section 5.4) | ☐ |

---

## 9. Appendix B — Command Reference

| Command | Purpose |
|---|---|
| `docker compose build` | Build all application images |
| `docker compose build frontend` | Rebuild the web client image only |
| `docker compose up -d` | Start the platform, or recreate changed services, in the background |
| `docker compose ps` | Report container status |
| `docker compose logs -f [service]` | Follow log output, optionally for one service |
| `docker compose stop` | Stop containers, retaining containers and data |
| `docker compose down` | Remove containers and the network, retaining images and data |
| `docker compose up -d --force-recreate` | Recreate all containers; required after changes to `kong.yml` |
| `docker compose exec [service] sh` | Open a shell in a running container for diagnostic purposes |

Service names: `frontend`, `kong`, `auth-service`, `document-reviewer`, `collateral-reviewer`,
`audit-service`.

> `docker compose pull` refreshes only the `kong:3`, `node:20-alpine`, and `nginx:1.27-alpine` base
> images. It does not retrieve application images. Use `docker compose build`.

---

## 10. Appendix C — Production Architecture Considerations

The single-server deployment described in this guide is the supported entry configuration. The following
considerations apply where high availability or additional security controls are required.

| Area | Single-server deployment | Consideration |
|---|---|---|
| Orchestration | Docker Compose on one server | Kubernetes or OpenShift for high availability |
| Secret management | `.env` file and supplied key files on the server | Injection from an enterprise secret management platform |
| Token signing | RS256 key pair supplied per deployment | Automated key rotation; private key sourced from a secret management platform |
| User store | SQLite database file | Managed PostgreSQL, federated with the customer identity provider |
| Service-to-service traffic | HTTP on the internal network | Mutual TLS |
| Image supply chain | Images built on the target server | Images built in a CI pipeline, security scanned, and distributed via an internal registry |
| Audit log | File on the local file system | Streamed to a SIEM platform or write-once storage |
