# CiviCRM Bridge

Luanti account/admission mod, restricted world controller and bearer-authenticated
HTTPS API used by the separate Luanti Portal. The historical project name does
not imply a working CiviCRM extension.

Status reflects source and tests as of 2026-10-17, not a check of the installed
server. **Implemented** means present in code; **Provisional** identifies moving
parts or host-specific assumptions; **Planned** is reserved for explicit future
work. Start with the [integration contract](docs/INTEGRATION.md),
[HTTPS API and deployment guide](docs/HTTPS_API.md), and
[example catalog](examples/README.md).

## Responsibilities and status

| Component | Implemented responsibility |
| --- | --- |
| Luanti Portal (separate project) | Portal users/organizations, permissions, approvals, reservations, account roster and job orchestration |
| Bridge HTTPS API | Backend bearer authentication, durable acceptance, account/lifecycle delivery, polling and recovery |
| Controller and publisher | Clean template bundles, private game copies, enabled access windows, archival and service transitions |
| Luanti mod | World-local account ownership, participant/facilitator privileges, admission windows, live kicks and durable receipts |

**Implemented:** the portal uses published templates and private instances via
lifecycle v2 and timed account v2 jobs. Each copy has separate authentication,
player data and bridge state. The API reconciles instance schedules approximately
every ten seconds; the mod enforces admission locally while Luanti runs. Legacy
`dev`/`test` lifecycle v1 commands and filesystem account imports remain available
for maintenance and tests.

**Provisional / moving parts:** host paths/identities, template publication and
installed-snapshot upgrades. The portal UI and Hetzner bootstrap also continue to
evolve. Portal VM provisioning verifies the engine only; it does not deploy this
bridge, a game/world, or public endpoints.

The Docker backend and separate preflight/apply installer are described in the
[Docker and Windows workflow](lifecycle/README.md#docker-backend). Native systemd
remains the default. Automatic production bridge deployment on VM rental remains
future work.

**Planned:** a CiviCRM component. Contact-to-account mapping, participation policy
and credential/retry storage still need decisions and implementation. See
[future CiviCRM decisions](docs/INTEGRATION.md#planned-civicrm-component).

## Deployment boundary

The existing recipe targets Ubuntu 24.04/Python 3.12, systemd and a separately
installed controller. The API runs as the unprivileged `civicrm_bridge_api` user
on `127.0.0.1:8765` behind an existing HTTPS reverse proxy, with one backend token
and exact sudo grants. Browsers do not receive the token. `api/install.py` prepares
review files; installation and template activation require operator action.

**Provisional host layout:** the development setup describes Luanti 5.17.0 with
LuaJIT/Minetest Game, `luanti-dev.service` running as `ben`, and UDP port 30000.
The source checkout is `/home/ben/.minetest/mods/civicrm_bridge`, server config
`/home/ben/luanti-dev/server.conf`, legacy world `/home/ben/luanti-dev/world`, and
private copies under `/home/ben/luanti-dev/instances`. These are recipe assumptions,
not portable defaults or proof of the current remote installation.

Enable `load_mod_civicrm_bridge = mods/civicrm_bridge` in each selected world's
`world.mt`. The shared mod must be available to the Luanti service user; do not
also install it as a world-local mod. The mod derives inbox/outbox paths from
`core.get_worldpath()`. The service user owns runtime writes; integration writers
receive inbox publication and read-only outbox access, not writes to world
settings, databases, receipts or checkpoints. Pending input can contain passwords.

Controller/API snapshots are root-owned; editing the checkout does not upgrade
them. The shared mod checkout remains trusted code executed by the service user.
See [lifecycle trust and upgrades](lifecycle/README.md#trust-boundary-and-upgrades)
and the HTTPS guide before changing permissions or activating a snapshot.

## Account behavior and local examples

`sync_accounts` creates bridge-owned accounts and synchronizes role/admission.
Participants receive `interact` and `shout`; facilitators also receive `fly`,
`fast` and `teleport`. Demotion revokes those managed privileges while preserving
unrelated manual grants. Existing unmanaged accounts, case collisions and the
configured administrator are protected.

Only admitted bridge accounts within any stored access window, plus the
administrator configured by Luanti's `name`, may join. Unknown players cannot
self-register. Withdrawal/expiry disconnects players and preserves passwords and
data. Initial passwords are ignored for existing managed accounts.

The [numbered examples](examples/README.md) and `tools/simulate.py` demonstrate
legacy account v1 only; the simulator defaults to the development world's
runtime. Use a disposable world and explicit `--runtime-dir`. It atomically
publishes one job, waits up to 15 seconds by default and prints a summary.
Exit codes are 0 success, 1 partial/error result, 2 input/filesystem error and
3 timeout. Timeout leaves queued input intact. Portal v2 contracts and exact-byte
retry rules are in the integration guide; do not use the v1 simulator for v2 jobs.

## Known limitations

- One world/game runs per configured service/port; overlapping enabled instance
  windows are rejected. Account imports do not start a stopped game.
- Durable acceptance is asynchronous. Account batches can partially succeed;
  uncertain outcomes require inspection before new dependent changes.
- Archival stops and permanently disables a copy while retaining its save. There
  is no save-deletion API or automatic receipt/archived-save expiry.
- No password reset, account deletion/rename, roster query or arbitrary privilege
  API exists. Template replacement starts fresh player progress; passwords changed
  in an older copy are not transferred by the portal.

## Verification and repository boundaries

Existing Linux checks use disposable worlds/state and loopback ports, not the
live development service: `tests/run_integration.py`, `tests/run_examples.py`,
`tests/run_lifecycle.py` (`--mock-only` skips engine processes),
`tests/run_world_security.py`, `tests/run_world_worker.py`, `tests/run_templates.py`,
`tests/run_instances.py` and `tests/run_docker_installer.py`.
API dependencies and checks are described in the HTTPS guide. Run these only in
their supported environment; their presence is not a claim that a deployment was
tested. Real-client login/admission checks remain an operator validation step.

Keep worlds, authentication databases, mod storage, runtime queues, receipts and
credentials out of Git. Back up runtime data and private API/registry state
separately. The code archive and contributor secret checks are described in
[GitHub archiving](tools/ARCHIVING.md); there is no automatic scheduled push.
Disabling the mod and restarting removes whitelist enforcement but does not undo
account/privilege changes already applied.