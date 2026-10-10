# HTTPS API: manual setup and backend integration

The API accepts account imports and world lifecycle jobs from one trusted
backend. Every API request requires one bearer token. Store it in the backend's
secret configuration; the browser never receives it. Luanti does not exchange
tokens with the backend. A token lasts until manually replaced or revoked.

Setup is **manual**, from the administrator's existing `ben` session.
`api/install.py` only prepares files for review in a new private directory. It
does not install packages, create accounts, change permissions/services/proxies,
or generate tokens. Keep deployment configuration and secrets outside Git.

## Architecture and prerequisites

```text
browser -> frontend backend (holds token) -> existing HTTPS proxy
        -> 127.0.0.1:8765 (API, running as civicrm_bridge_api)
           |-> selected world's account inbox/outbox
           |-> exact sudo command -> civicrm-worldctl
```

Use your existing reverse proxy and certificate management. The API listens on
loopback only. This recipe targets Ubuntu 24.04, Python 3.12, the existing
`dev`/`test` worlds and installed world controller. It does not restart Luanti.
If the proxy is containerized, its `127.0.0.1` is not the host: arrange a private
host connection first. Do not expose port 8765 publicly.

Install only these prerequisites if missing:

```bash
sudo apt-get install python3-venv acl
```

No proxy package installation is required. `bridge.example.org` below is a
placeholder for the hostname managed by your existing hosting environment.

## 1. Prepare configuration without sudo

From the authoritative checkout as `ben`:

```bash
cd /home/ben/.minetest/mods/civicrm_bridge
python3 api/install.py --hostname bridge.example.org \
  --output /home/ben/bridge-api-setup
```

The output directory must be new and outside Git. Review its six files:

| File | Purpose |
| --- | --- |
| `config.json` | Hostname, fixed runtime paths, token/state paths and `ben` UID |
| `civicrm-bridge-api.service` | API identity, loopback listener and sandbox |
| `civicrm-bridge-api.sudoers` | Only the six existing controller argument lists |
| `civicrm-bridge-api.tmpfiles` | Root-owned controller lock recreated after reboot |
| `civicrm-bridge-token` | Local administrator CLI wrapper |
| `token_cli.py` | Imports only the installed API code snapshot |

The hostname records the intended public endpoint; routing and TLS enforcement
belong to the existing proxy. The following commands are for **first setup**.
For upgrades, preserve existing configuration, token and state as described below.

## 2. Create the API identity and prepare permissions

Create a separate unprivileged service account if absent:

```bash
sudo useradd --system --user-group --no-create-home \
  --home-dir /var/lib/civicrm-bridge-api \
  --shell /usr/sbin/nologin civicrm_bridge_api
```

If the account already exists, inspect it first: it needs a non-root UID and no
login shell. The API must not run as `ben` or the developer SSH account.

Stop publishing imports during the permission migration. From `ben`, run the
reviewed updated lifecycle installer and grant traversal of the private home:

```bash
sudo /usr/bin/python3 -I /home/ben/.minetest/mods/civicrm_bridge/lifecycle/install.py
sudo setfacl -m u:civicrm_bridge_api:--x /home/ben
```

The lifecycle installer detects the API account and grants traversal, inbox
publication and read-only outbox access. World settings, receipts, checkpoint,
databases and server logs remain inaccessible. It retains the selected world
and does not restart Luanti. Later installer runs retain these API world ACLs
while that service account exists.

## 3. Install the reviewed code snapshot

Review any existing objects before using these exact paths. Code/configuration
are root-owned; state is private and owned by the API identity:

```bash
sudo install -d -o root -g root -m 0755 \
  /usr/local/lib/civicrm-bridge-api \
  /usr/local/lib/civicrm-bridge-api/api \
  /usr/local/lib/civicrm-bridge-api/tools \
  /etc/civicrm-bridge-api
sudo install -d -o civicrm_bridge_api -g civicrm_bridge_api -m 0700 \
  /var/lib/civicrm-bridge-api

for file in __init__.py security.py contract.py tokens.py jobs.py app.py serve.py; do
  sudo install -o root -g root -m 0644 "api/$file" \
    "/usr/local/lib/civicrm-bridge-api/api/$file"
done
sudo install -o root -g root -m 0644 tools/world_worker.py \
  /usr/local/lib/civicrm-bridge-api/tools/world_worker.py
sudo install -o root -g root -m 0644 api/requirements.txt \
  /usr/local/lib/civicrm-bridge-api/api/requirements.txt
sudo /usr/bin/python3 -m venv /usr/local/lib/civicrm-bridge-api/venv
sudo /usr/local/lib/civicrm-bridge-api/venv/bin/python -I -m pip install \
  --only-binary=:all: \
  -r /usr/local/lib/civicrm-bridge-api/api/requirements.txt
```

Install the prepared configuration after review:

```bash
sudo install -o root -g root -m 0644 /home/ben/bridge-api-setup/config.json \
  /etc/civicrm-bridge-api/config.json
sudo install -o root -g root -m 0644 /home/ben/bridge-api-setup/token_cli.py \
  /usr/local/lib/civicrm-bridge-api/api/token_cli.py
sudo install -o root -g root -m 0755 /home/ben/bridge-api-setup/civicrm-bridge-token \
  /usr/local/sbin/civicrm-bridge-token
sudo visudo -cf /home/ben/bridge-api-setup/civicrm-bridge-api.sudoers
sudo install -o root -g root -m 0440 /home/ben/bridge-api-setup/civicrm-bridge-api.sudoers \
  /etc/sudoers.d/civicrm-bridge-api
sudo visudo -cf /etc/sudoers
sudo install -o root -g root -m 0644 /home/ben/bridge-api-setup/civicrm-bridge-api.service \
  /etc/systemd/system/civicrm-bridge-api.service
sudo install -o root -g root -m 0644 /home/ben/bridge-api-setup/civicrm-bridge-api.tmpfiles \
  /etc/tmpfiles.d/civicrm-bridge-api.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/civicrm-bridge-api.conf
sudo systemctl daemon-reload
sudo systemd-analyze verify civicrm-bridge-api.service
```

The service executes the installed snapshot, not the developer-writable checkout.
Keep world data and the local workspace's `AGENTS.md` out of this deployment.
The unit deliberately permits the setuid transition required by restricted sudo;
adding `NoNewPrivileges=true` would break lifecycle operations. Its sandbox lets
the root-owned controller write its selection state and lock. Filesystem
permissions still deny the API identity direct access to those privileged writes.

## 4. Generate the backend token and start the API

From `ben`, using local administrator sudo:

```bash
sudo /usr/local/sbin/civicrm-bridge-token generate \
  --output /root/civicrm-bridge-backend.token
sudo /usr/local/sbin/civicrm-bridge-token status
sudo systemctl enable --now civicrm-bridge-api.service
sudo systemctl status civicrm-bridge-api.service --no-pager
```

The delivery file must not already exist. It is mode `0600`, and the command
never prints the secret. Transfer its contents through your private administrator
workflow to the backend's secret configuration, then remove the delivery file.
Do not put tokens in browser code, URLs, JSON jobs, logs or Git.

Only the SHA-256 digest remains in `/etc/civicrm-bridge-api/token.json`: root-owned,
mode `0640`, group `civicrm_bridge_api`. The API reloads it for every request.
Missing, corrupt or revoked token state denies all requests. Administration is
local CLI only, never an HTTP endpoint.

Check the local listener without a token:

```bash
curl -i http://127.0.0.1:8765/v1/lifecycle/jobs
```

Expect `401 Unauthorized`. This private upstream check is the only place this
recipe uses HTTP; backend requests must use the public HTTPS endpoint.

## 5. Connect your existing reverse proxy

Use upstream `http://127.0.0.1:8765`. Preserve `/v1/...` paths and the
`Authorization` and `Content-Type` headers. Disable caching and logging of
authorization headers/request bodies. External plain HTTP must not forward
API traffic. Continue using your existing certificate management.

Example location block for an existing **Nginx HTTPS virtual host**, to review
and adapt through your hosting environment's supported mechanism:

```nginx
location /v1/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header Host $host;
    proxy_http_version 1.1;
    proxy_cache off;
    proxy_request_buffering off;
    client_max_body_size 256k;
    proxy_read_timeout 30s;
    access_log off;
}
```

The `proxy_pass` has no URI suffix, so the original path is preserved. This is
not a replacement server configuration. Validate/reload through your existing
hosting workflow; do not replace unrelated proxy routes or Apache sites. See
[Nginx's proxy documentation](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

Verify HTTPS with normal certificate validation: an unauthenticated request
returns `401`; an authenticated unknown job returns `404`. Then submit a fresh
`status` job with the valid token and poll its result. This does not change the
active world. Do not use start/stop/switch as installation health checks.

## API and backend usage

Send `Authorization: Bearer TOKEN` on every request, plus `Content-Type:
application/json` on POSTs. Authentication runs before routing/body parsing.
There are no public health/docs routes, browser CORS credentials, token cookies
or query-string tokens.

| Endpoint | Behavior |
| --- | --- |
| `POST /v1/worlds/{world_id}/accounts/jobs` | Account v1 JSON, world `dev` or `test` |
| `GET /v1/worlds/{world_id}/accounts/jobs/{job_id}` | Account progress/result |
| `POST /v1/lifecycle/jobs` | Lifecycle v1 JSON |
| `GET /v1/lifecycle/jobs/{job_id}` | Lifecycle progress/result |

Use the account schema in [INTEGRATION.md](INTEGRATION.md#account-request-v1)
and lifecycle examples in [the example catalog](../examples/lifecycle/README.md).
The API additionally rejects unknown JSON fields, duplicate keys, non-finite
values, invalid usernames, roles, admission types and supplied passwords.
Only `sync_accounts` and the four existing lifecycle operations are supported.
Start/switch require `world_id: dev` or `test`; arbitrary paths, commands, games
and service names are not request parameters. Limits are 256 KiB for accounts
and 16 KiB for lifecycle jobs. Compressed request bodies are rejected.

Durably accepted POSTs return `202`, a relative `Location` header and an envelope:

```json
{"version":1,"job_id":"backend-status-001","state":"pending","result_url":"/v1/lifecycle/jobs/backend-status-001"}
```

Poll `result_url` with the same bearer header. GET returns `200` and `state`
equal to `pending`, `completed` or `outcome_unknown`. Terminal envelopes add
`result`, containing the original account/controller response. Account envelopes
also include `world_id`. Completion is not necessarily success: inspect account
`status`/per-account outcomes or lifecycle `ok`, `ready`, world and rollback fields.

| HTTP status | Meaning |
| --- | --- |
| `200` | GET, or identical retry of a terminal job |
| `202` | Accepted or identical retry still pending |
| `400` | Invalid JSON/schema/operation |
| `401` | Missing, invalid or revoked bearer token |
| `404` | Unknown world/job/route |
| `409` | Same API job ID with different exact bytes |
| `413` | Request too large |
| `415` | Wrong media type or unsupported content encoding |
| `503` | State unavailable; inspect outcome before new decisions |

Python backend example using only the standard library. `token_file` is a secret
file on the backend, not the API's digest file:

```python
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
import uuid

base_url = "https://bridge.example.org"
token_file = Path("/private/backend/luanti-bridge.token")
headers = {"Authorization": "Bearer " + token_file.read_text().strip(),
           "Content-Type": "application/json"}
job_id = "backend-status-" + uuid.uuid4().hex
raw = json.dumps({"version": 1, "job_id": job_id, "operation": "status"}).encode()
# Persist this ID, endpoint and exact raw bytes securely BEFORE delivery.
# A transport retry must reuse those bytes; a new status query needs a new ID.
request = Request(base_url + "/v1/lifecycle/jobs", data=raw,
                  headers=headers, method="POST")
with urlopen(request, timeout=30) as response:
    job = json.load(response)
deadline = time.monotonic() + 660
while job["state"] == "pending" and time.monotonic() < deadline:
    time.sleep(1)
    with urlopen(Request(base_url + job["result_url"], headers=headers), timeout=30) as response:
        job = json.load(response)
if job["state"] == "pending":
    raise TimeoutError("Still pending; retain the job and poll later")
if job["state"] == "outcome_unknown":
    raise RuntimeError("Inspect fresh status before another mutating decision")
print(job["result"])  # Results only, never headers or submitted passwords.
```

For accounts, change the POST URL to `/v1/worlds/test/accounts/jobs` and use an
account example with a fresh ID. A valid batch can partially succeed. Existing
passwords are preserved; admission is independent of role; withdrawal kicks
online players and retains their account/data.

## Retry, ordering and crash recovery

Account IDs are world-scoped; lifecycle IDs are global. Identical bytes with
the same ID return the original job. Changed whitespace conflicts too. A replay
of an old status job returns its historical snapshot, not current world status.

Account publication is atomic and never overwrites pending input. Old outbox
results are ignored until the inbox file is consumed. Jobs for a stopped world
stay pending until it runs; imports never implicitly start/switch worlds. The
mod retains filename ordering, so wait for dependent decisions to complete
before submitting the next one.

Undispatched accepted jobs resume after API restart. Interrupted lifecycle
dispatch replays a worker receipt if one exists; otherwise it reports
`outcome_unknown` without executing again. Ambiguous account publication also
reports unknown instead of assuming an old outbox proves completion. A network
timeout is not proof of rejection. Never delete state to force replay.

Use a fresh status job and operator inspection to resolve uncertain outcomes;
the controller may still be completing. Check rollback details after failed
starts/switches. Resolve uncertainty before issuing a new mutating decision.
After terminal completion/unknown, API request blobs are removed; private
pending account jobs can contain initial passwords. SQLite uses secure deletion
of removed request blobs and no persistent WAL.

Back up `/var/lib/civicrm-bridge-api` privately with the world bridge state.
`jobs.sqlite3` and `lifecycle/receipts.sqlite3` are separate from the CLI worker's
home-directory receipts. Losing API state loses its acceptance/deduplication
history. Completed records have no automatic expiry.

## Token replacement and revocation

Replace only when needed, using a new private delivery filename:

```bash
sudo /usr/local/sbin/civicrm-bridge-token generate \
  --output /root/civicrm-bridge-backend-replacement.token
sudo /usr/local/sbin/civicrm-bridge-token status
```

The old token becomes invalid immediately without an API restart. Update the
backend secret so its requests can resume. There is no overlap/automatic expiry.
Accepted server jobs continue after rotation or revocation. To deny new traffic:

```bash
sudo /usr/local/sbin/civicrm-bridge-token revoke
```

Revocation retains disabled token state. Upgrades preserve it; only an explicit
`generate` command reauthorizes requests.

## Upgrades, troubleshooting and checks

For upgrades, stop only `civicrm-bridge-api.service`, privately back up installed
code/configuration/state, then copy reviewed code and update dependencies. Keep
`config.json`, `token.json` and all API state. Review any replacement unit/sudoers
files, validate them, run `daemon-reload`, and restart only the API. Do not
generate a token during an upgrade. Shutdown waits for an in-flight lifecycle
worker; forced interruption is recovered conservatively. Roll back code with
its matching dependency environment while retaining state and token configuration.

Administrator checks:

```bash
sudo systemctl status civicrm-bridge-api.service --no-pager
sudo journalctl -u civicrm-bridge-api.service --since "10 minutes ago" --no-pager
sudo /usr/local/sbin/civicrm-bridge-token status
```

`401` indicates bearer/header/digest configuration. Proxy `502` indicates upstream
reachability or API startup failure. Pending imports can mean a stopped world
or halted imports/storage problems. Check narrowly required ACLs; do not grant
broad world writes or general sudo to fix configuration mistakes.

Automated checks run as the dedicated developer account using temporary state,
loopback ports and disposable worlds, never the live controller/service:

```bash
python3 -m venv ~/.cache/civicrm-bridge-api-tests/venv
~/.cache/civicrm-bridge-api-tests/venv/bin/python -m pip install -r api/requirements-test.txt
~/.cache/civicrm-bridge-api-tests/venv/bin/python tests/run_api.py
python3 tests/run_world_worker.py
python3 tests/run_world_security.py
python3 tests/run_lifecycle.py --mock-only
python3 -I tools/check_archive.py
```

Set `CADDY_BIN` to an existing unprivileged test binary to include the optional
real HTTPS proxy test. It uses temporary certificates and random loopback ports
and does not install/configure production proxy services. The real-engine API
fixture checks import, password preservation, withdrawal, role/admission
independence and replay in a disposable Luanti world.
