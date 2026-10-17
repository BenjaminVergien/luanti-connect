# HTTPS API: manual setup and backend integration

**Implemented:** Luanti Portal uses this API for private-instance lifecycle v2
and timed account v2 jobs. The `/v1/` route prefix and job envelopes remain v1;
payload/result versions are separate. See the [integration contract](INTEGRATION.md)
for component responsibilities, schemas and the approval-to-game workflow.

**Provisional / moving parts:** the installation below is a host-specific
Ubuntu/systemd recipe, not evidence that a remote service is upgraded. Steps 1-5
prepare the base service; portal use also requires the private-game controller/API
upgrade and published templates described under API usage. A portal-provisioned
Hetzner VM with a verified Docker engine does not yet contain this deployment.

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
browser -> Luanti Portal backend (holds token) -> existing HTTPS proxy
        -> 127.0.0.1:8765 (API, running as civicrm_bridge_api)
           |-> registered private instance's account inbox/outbox
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
  /usr/local/lib/civicrm-bridge-api/lifecycle \
  /etc/civicrm-bridge-api
sudo install -d -o civicrm_bridge_api -g civicrm_bridge_api -m 0700 \
  /var/lib/civicrm-bridge-api

for file in __init__.py security.py contract.py tokens.py jobs.py worlds.py app.py serve.py; do
  sudo install -o root -g root -m 0644 "api/$file" \
    "/usr/local/lib/civicrm-bridge-api/api/$file"
done
for file in __init__.py controller.py instances.py instance_contract.py templates.py; do
  sudo install -o root -g root -m 0644 "lifecycle/$file" \
    "/usr/local/lib/civicrm-bridge-api/lifecycle/$file"
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
| `GET /v1/worlds` | Existing saved maps from the configured world allowlist; independent of running/stopped status |
| `GET /v1/world-templates` | Published clean templates with stable IDs and SHA-256 revisions |
| `POST /v1/worlds/{world_id}/accounts/jobs` | Account v1/v2 JSON; target must be a registered private game instance |
| `GET /v1/worlds/{world_id}/accounts/jobs/{job_id}` | Account progress/result |
| `POST /v1/lifecycle/jobs` | Legacy lifecycle v1 or private-instance lifecycle v2 JSON |
| `GET /v1/lifecycle/jobs/{job_id}` | Lifecycle progress/result |

For private-instance polling, `{world_id}` in the historical account route is
the registered `instance_id`; the account JSON itself has no routing field.
Template catalogues contain at most 1000 entries; each entry's fields and the
portal workflow are specified in the integration contract.

The legacy `/v1/worlds` catalogue returns `{"version":1,"worlds":[{"world_id":"dev","name":"dev"},{"world_id":"test","name":"test"}]}`.
It checks for each configured map's regular `world.mt` file without starting the
engine or creating a job. Unconfigured directories and filesystem paths are
never returned. Removed maps are omitted; an empty list is a valid catalogue.
Inspection failures return `503` with `world_catalogue_unavailable`, rather than
publishing a partial list. Backends should retain their previous catalogue on
failed fetches. Names in this legacy response use configured IDs. The portal
instead synchronizes `/v1/world-templates`, retaining local display/selection
settings and marking missing templates unavailable only after a successful fetch.

Use the [account v1/v2 schema](INTEGRATION.md#account-request-v1v2) and
[private-instance v2 schema](INTEGRATION.md#private-instance-lifecycle-v2).
The [example catalog](../examples/lifecycle/README.md) contains legacy v1
maintenance fixtures, not the portal's private-copy allocation workflow.
The API additionally rejects unknown JSON fields, duplicate keys, non-finite
values, invalid usernames, roles, admission types and supplied passwords.
`sync_accounts` targets registered private copies; template and legacy saved-world
identifiers are rejected by the account API. Lifecycle v1 remains available for
operator maintenance of `dev` and `test`.

Lifecycle v2 accepts `create_instance`, `configure_instance`, `start_instance`,
`stop_instance`, `instance_status`, and `archive_instance`. The backend persists
the instance ID before creation. A copy starts disabled until configured; archival
permanently disables it while retaining its save. The bridge reconciles schedules
approximately every ten seconds while the API runs, including after restart;
overlapping enabled windows are rejected. Timed account v2 adds a required
`access_window` per account, enforced locally by the mod even without a fresh
portal withdrawal. Keep clocks synchronized. Full fields/limits are in the
integration contract.

For publication, first upgrade the reviewed lifecycle installer from the `ben`
session. It installs the root-owned publisher and prepares separate template,
instance, and registry directories:

```bash
sudo /usr/bin/python3 -I /home/ben/.minetest/mods/civicrm_bridge/lifecycle/install.py
```

To explicitly use the existing `dev` and `test` maps as templates, run this reviewed
export from the same `ben` session:

```bash
sudo /usr/bin/python3 -I /home/ben/.minetest/mods/civicrm_bridge/tools/export_legacy_templates.py
```

The export holds the controller lock, briefly stops a running legacy world, and
restores it even if publication fails. It keeps the original saves and publishes
`dev` and `test` at `/home/ben/luanti-dev/templates/dev-v1` and `test-v1`. Only
`world.mt`, the SQLite map snapshot, and optional map/environment metadata are
included. Accounts, player databases, all mod storage, world-local code and bridge
runtime are excluded; player progress starts fresh. This deliberately omits mod
storage rather than assuming old mod state is safe to reuse. Existing valid
publications are retained on retries. The command prints the paths but does not
change controller/API configuration or restart the API. After exporting both maps,
activate their reviewed private-game API upgrade from `ben`:

```bash
sudo /usr/bin/python3 -I /home/ben/.minetest/mods/civicrm_bridge/api/upgrade.py
```

This upgrade registers `dev`/`test` in both configurations, installs the reviewed
API code snapshot and exact instance-controller sudo permissions, and restarts an
already running API. It retains the token, job state, existing configuration
values and the running game. It also repairs inherited bundle ACLs so the API can
read template content. Backups and ACLs are retained under
`/var/lib/civicrm-worldctl/private-games-backup-*`; failed activation restores
the previous installed files and ACLs. Custom token/state paths, conflicting
template registrations and unrecognized managed-file changes require separate
review. Future controller upgrades retain registered template/revision mappings.

For a separately prepared template, publish a stopped, clean SQLite world directory
(replace `/path/to/reviewed-clean-world` with its actual path):

```bash
sudo /usr/bin/python3 -I /usr/local/lib/civicrm-worldctl/publish_template.py \
  --source /path/to/reviewed-clean-world \
  --destination /home/ben/luanti-dev/templates/classroom-v1 \
  --template-id classroom --name 'Classroom'
```

The source must contain `world.mt` and `map.sqlite`. Optional files are
`map_meta.txt`, `env_meta.txt`, and reviewed `mod_storage.sqlite` without bridge
state. Authentication/player databases, world-local code, logs, runtime directories,
and SQLite journals are rejected. Published destinations cannot be overwritten.
Register the same `templates` mapping in the root-owned controller and API
configurations, e.g. `{"classroom":"/home/ben/luanti-dev/templates/classroom-v1"}`.
Retain immutable revision directories referenced by existing applications.
When publishing a newer revision under the same template ID, list retained paths
in the controller's `template_revisions` mapping, for example
`{"classroom":["/home/ben/luanti-dev/templates/classroom-v1"]}`, so interrupted
creation can still resolve the exact approved revision.
Restart the API after installing its reviewed code/configuration snapshot; do not
restart the game service to publish a template. These setup steps require the
operator's `ben` session; the integration account has no general sudo permission.

The legacy start/switch operations retain these restrictions:
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
also include `world_id` (the private instance ID for portal jobs). Completion is
not necessarily success: inspect account `status`/per-account outcomes, instance
v2 `ok`/`state`/`error.code`, or legacy v1 readiness/world/rollback fields. There is
no browser/public health route. The portal's authenticated unknown-job probe checks
API reachability/token acceptance, not game readiness; its HTTP client enforces
HTTPS, disables redirects and bounds response downloads to 256 KiB.

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

Python backend example using only the standard library. Replace the fictitious
instance ID with an already registered private copy. `token_file` is a private
backend secret file, not the API's digest file:

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
instance_id = "g00000000000000000000000000"  # Replace with the saved instance ID.
raw = json.dumps({"version": 2, "job_id": job_id,
                  "operation": "instance_status", "instance_id": instance_id}).encode()
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

For accounts, POST the [account v2 example](INTEGRATION.md#account-request-v1v2)
with a fresh ID to `/v1/worlds/g00000000000000000000000000/accounts/jobs`, replacing
that fictitious ID with the registered private instance. Poll its returned job
path. Legacy `test`/`dev` IDs and template IDs are rejected by this account route.
A valid batch can partially succeed; existing passwords are preserved, and
withdrawal/expiry kicks players while retaining accounts/data.

## Retry, ordering and crash recovery

Account job IDs are instance-scoped; lifecycle job IDs are global. Identical bytes with
the same ID return the original job. Changed whitespace conflicts too. A replay
of an old status job returns its historical snapshot, not current world status.

Account publication is atomic and never overwrites pending input. Old outbox
results are ignored until the inbox file is consumed. Jobs for a stopped world
stay pending until it runs; imports never implicitly start/switch worlds. The
mod retains filename ordering, so wait for dependent decisions to complete
before submitting the next one.

Undispatched accepted jobs resume after API restart. Interrupted lifecycle
dispatch replays a terminal worker receipt if one exists. An interrupted v1
request otherwise reports `outcome_unknown` without executing again; v2 instance
requests can resume through the controller's instance identity/generation checks.
A recorded terminal result remains a replay, not a fresh execution. Ambiguous
account publication reports unknown rather than assuming an old outbox proves
completion. A timeout is not proof of rejection. Never delete state to force replay.

Use a fresh status job and operator inspection to resolve uncertain outcomes;
the controller may still be completing. Check rollback details after failed
starts/switches. Resolve uncertainty before issuing a new mutating decision.
After terminal completion/unknown, API request blobs are removed; private
pending account jobs can contain initial passwords. SQLite uses secure deletion
of removed request blobs and no persistent WAL.

Back up `/var/lib/civicrm-bridge-api` privately with saves, world bridge state,
the controller registry/selection/configuration and referenced template revisions.
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

For the private-instance upgrade, copy the reviewed `lifecycle` helper modules
into the installed API snapshot as shown above. Preserve the existing API
configuration and add `instances_root: "/home/ben/luanti-dev/instances"`,
`instance_registry: "/var/lib/civicrm-worldctl/instances"`, and the published
`templates` mapping. Apply the reviewed unit/sudoers changes for the fixed
`instance` and `reconcile_instances` commands and the instance inbox sandbox.
This feature adds no runtime dependencies and does not require token replacement.

Keep backend and bridge versions compatible when rolling back: old v1-only
snapshots do not implement private-instance routing/timed accounts. A source
checkout update alone does not upgrade installed code. The separate portal's
template migration requires a destructive local reset for existing development
records; that is not a production data-conversion procedure.

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

## Docker development endpoint

The existing HTTP routes and account/lifecycle job versions also serve Docker installations. Idle status may have `selected_world_id: null`; private-instance operations retain their contracts. See [Docker installation and Windows development](../lifecycle/README.md#docker-backend) for the separate installer and `bridge check/test/up` commands. The development API and HTTPS proxy bind to VM loopback only and are reached through pinned SSH at `https://localhost:18443`. The local portal trusts the deployment's public CA only for this exact origin in `APP_ENV=local`; certificate verification is never disabled.
