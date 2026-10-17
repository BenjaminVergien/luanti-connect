# World controller and legacy development lifecycle

**Implemented:** the controller manages both private game instances (lifecycle v2, used by the portal) and legacy `dev`/`test` maintenance (v1). There is one Luanti process and one public UDP port, 30000, in this recipe. Switching stops the current world and disconnects players.

The private-instance [contract](../docs/INTEGRATION.md#private-instance-lifecycle-v2) covers clean-copy creation, enabled schedules, status, stop/start and archival. The [HTTPS guide](../docs/HTTPS_API.md#api-and-backend-usage) covers publication and installed API activation. Copies start disabled, overlapping enabled windows are rejected, and archival retains the save. API schedule reconciliation needs the API service running; manual stop alone does not disable an enabled schedule.

**Provisional / moving parts:** the paths, users and service below describe the existing Linux/systemd recipe. Installing the reviewed source is a separate operator action; this document does not verify the installed server or deploy a portal-provisioned Docker engine as a working bridge.

## Legacy worlds

| ID | World directory |
| --- | --- |
| `dev` | `/home/ben/luanti-dev/world` |
| `test` | `/home/ben/luanti-dev/worlds/test` |

Both legacy worlds load the shared mod at `/home/ben/.minetest/mods/civicrm_bridge`. Each world has separate accounts, player data, bridge admission state, receipts, inbox and outbox. The installer creates a fresh test world if absent. The v1 local import examples still work with an explicit runtime directory; HTTPS account imports instead require registered private instances. Private copies also load the shared mod and do not inherit authentication/player/bridge state from templates.

## Docker backend

Ubuntu 24.04, local rootful Docker and no user-namespace remapping are the initial supported environment. The API and restricted controller stay on the host; only Luanti and the Lua mod run in one managed container. Missing `backend` retains native systemd behavior. A fresh Docker installation has no artificial worlds and may report `selected_world_id: null`.

Supply reviewed root-owned bridge source, Minetest Game files, an already-pulled official image and an external root-owned configuration file:

```json
{
  "image": "ghcr.io/luanti-org/luanti:5.17.0",
  "game_source": "/root/reviewed/minetest_game",
  "admin_name": "Administrator",
  "hostname": "bridge.example.org",
  "templates": {}
}
```

```bash
sudo python3 -I /root/reviewed/bridge/lifecycle/install_docker.py --config /root/bridge-installation.json
sudo python3 -I /root/reviewed/bridge/lifecycle/install_docker.py --config /root/bridge-installation.json --apply
```

The default performs preflight and a disposable engine probe without changing services. `--apply` installs code, configuration, identities, exact sudo grants and services. Docker, systemd, Python 3/venv, sudo and ACL tools must already be installed. Templates may initially be empty; map IDs to explicitly supplied published bundle directories. No automatic game downloads or native data migration are provided.

The root-owned configuration fixes the immutable image ID, installation UUID, container name, command, mounts, UID/GID (30000:30000 by default) and ports. API payloads cannot select them. The API runs unprivileged on 127.0.0.1:8765; only the restricted controller uses Docker. Neither API nor game receives the Docker socket. World ACLs permit inbox publication and outbox reading while settings, receipts and databases remain private.

The image, home skeleton, supplied game, server configuration and three Lua mod files are read-only. Only the selected world is a writable bind mount; `/tmp` is bounded scratch space. The home includes an empty, read-only `mod_data` directory required by Luanti 5.17; Minetest Game uses world-local mod storage. Capabilities are dropped, privilege escalation is disabled and startup sets umask 0077. Declared image volumes are explicitly covered.

Before controlling or removing a container, inspection verifies the persisted container ID, launch UUID, labels, image, command, user, mounts, ports and security options. Unknown or changed containers remain untouched. Ambiguous CLI results are inspected before retrying. Readiness requires the bridge-ready and engine-listening messages from the current container start; cached evidence is tied to its ID and exact start timestamp.

Graceful shutdown, lifecycle locking, rollback, enabled schedules and archival remain in force. Private-instance approval is rechecked before creation and again immediately before starting the engine, including after a slow Docker operation. Docker has no automatic restart policy: bridge reconciliation decides whether a game may run again. Container removal never deletes world directories.

Identical installations preserve tokens, jobs, registrations, saves and revisions. Changed code/configuration requires a stopped game and recognized managed hashes/modes. Published revisions are retained. Root-owned backups remain under `/var/lib/civicrm-worldctl/docker-backup-*`; publication/validation failures restore previous managed files. Pinned dependencies use separate environments keyed by the requirements checksum.

## Windows development and private Hetzner testing

Windows `civicrm_bridge/` is a real Git checkout. Remote uncommitted private-instance changes were preserved and compared before the move; newer local documentation was retained. Use local GitHub authentication, never the old remote deploy key. Keep bridge and portal repositories separate. The mandatory archive hook discovers working Python 3 on Windows/Linux and requires Gitleaks.

From the sibling portal:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev.ps1 bridge check
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev.ps1 bridge test --server=4 --game-source=C:\reviewed\minetest_game
powershell -NoProfile -ExecutionPolicy Bypass -File .\dev.ps1 bridge up --server=4 --game-source=C:\reviewed\minetest_game
```

`--server` is the portal record ID, not the Hetzner resource ID. Local overrides are `BRIDGE_DEV_SOURCE`, `BRIDGE_DEV_GAME_SOURCE` and `BRIDGE_DEV_PYTHON`. Python 3 and Gitleaks must be on PATH. VM keys are reused through the portal's encrypted storage, pinned host identity and provider ownership checks; no manual key export is needed.

`check` runs portable backend/configuration/package/hook tests, mandatory full-history scanning and a secret scan of pending source. Linux suites are reported separately. `test` uploads a checksummed allowlisted snapshot and explicit game content, then runs Linux/ACL/API/systemd-regression and actual Docker/engine suites using per-run paths, containers and ports. Native engine fixtures use a test-only container adapter; production isolation has separate non-root tests. Git metadata, workspace instructions, credentials and world data are excluded. Tests never use the old development server.

`up` accepts only the exact tested source, game checksum and image ID. It applies the Docker installer, starts the private HTTPS proxy and opens a foreground SSH tunnel from Windows 127.0.0.1:18443 to VM 127.0.0.1:8443. The API/proxy bind only to VM loopback. The public test CA and deployment-specific bearer token transfer through pinned SSH; the portal stores the token encrypted. Its HTTP client trusts this CA only in `APP_ENV=local` for `https://localhost:18443`, with certificate verification enabled. Ctrl+C closes the tunnel and removes temporary SSH files; remote saves and schedules remain. Public DNS/TLS and automatic bridge deployment during production VM rental remain separate work.

## Native one-time installation

Review the lifecycle scripts, then run this from the existing `ben` SSH session:

```bash
sudo /usr/bin/python3 -I /home/ben/.minetest/mods/civicrm_bridge/lifecycle/install.py
```

The installer verifies that the existing service runs as `ben` and targets the development world, and creates a fresh test world if needed. It removes legacy developer-write ACLs from the configured worlds and their development-tree parents, transfers developer-owned world objects to `ben`, and protects `world.mt`, world data, server configuration, and the runtime checkpoint/receipts. The integration account can traverse to each runtime inbox and publish jobs there, and can read outbox results. It cannot modify results, receipts, executable world settings, or world-local mods. Preparation uses held no-follow descriptors, rejects symlinks and hard-linked world files, and checks held parent/child identities before applying permissions. Nonempty `worldmods` directories must be removed or reviewed before installation. It installs reviewed copies of the controller under `/usr/local/lib/civicrm-worldctl`, a wrapper at `/usr/local/sbin/civicrm-worldctl`, configuration under `/etc/civicrm-worldctl`, selected-world state under `/var/lib/civicrm-worldctl`, and a systemd drop-in. These installed files are root-owned; editing the Git checkout does not change privileged controller code.

The installer validates sudoers before activation, reloads systemd, and verifies the resulting service configuration. It does **not** restart the running service. The first installation selects `dev`; reinstallations retain the selected world. Unrecognized files or locally modified managed files cause installation to stop rather than overwrite them. Recognized upgrades retain backups under `/var/lib/civicrm-worldctl/backup-*` and restore managed files if verification fails.

## Trust boundary and upgrades

The dedicated account retains trusted source-development access to the shared `civicrm_bridge` checkout. That source is executed by Luanti as `ben`; this permission repair closes the world-local mod path and does not isolate an untrusted source developer from the service identity. A deployment that treats this account as untrusted needs a separate reviewed, non-writable deployed mod copy or a separate test-service identity.

The controller checks ownership and effective access/default ACLs along the entire world path and on `world.mt` on every start/switch, and the service launcher repeats validation before executing Luanti. World-local mods are rejected even if disabled; symlinked or dangling `worldmods` directories are rejected too. Only root and the configured service user may have write access to executable world paths. Validation happens before the current service is stopped. Game and shared mods remain the administrator/developer-reviewed source locations.

Stop publishing imports during the installer upgrade so its existing-file migration sees stable files. The installer retains an ACL snapshot at `permissions.acl` in its root-only backup directory before repairing permissions. If later installation checks fail, restrictive ACL repairs remain in place; managed controller files are restored, but the installer never automatically reopens developer writes. An administrator can inspect the snapshot for targeted recovery. Restoring the old ACLs would also restore the reported vulnerability. Editing the checkout alone does not upgrade the root-owned installed controller; run the reviewed installer from the `ben` session after testing.

## HTTPS API identity

The [HTTPS API](../docs/HTTPS_API.md), required for portal integration, runs as `civicrm_bridge_api` in this recipe.
When that account exists, the lifecycle installer preserves its traversal,
inbox publication and read-only outbox ACLs, while denying world configuration,
receipts, checkpoint and server-log access. API setup and its exact sudo grants
are manual; the lifecycle installer does not install or start the API.

## Commands

For validated JSON input, run the unprivileged [local lifecycle worker](../tools/WORLD_WORKER.md): `python3 tools/world_worker.py REQUEST.json`. Its private receipts are separate from API/instance state. Legacy v1 uses the six maintenance commands below; v2 requires the updated installer grants for the fixed `instance` operation, with strict JSON on stdin.

The dedicated developer account receives passwordless sudo access only to these exact commands:

```bash
sudo -n /usr/local/sbin/civicrm-worldctl status
sudo -n /usr/local/sbin/civicrm-worldctl stop_world
sudo -n /usr/local/sbin/civicrm-worldctl start_world dev
sudo -n /usr/local/sbin/civicrm-worldctl start_world test
sudo -n /usr/local/sbin/civicrm-worldctl switch_world dev
sudo -n /usr/local/sbin/civicrm-worldctl switch_world test
sudo -n /usr/local/sbin/civicrm-worldctl instance
sudo -n /usr/local/sbin/civicrm-worldctl reconcile_instances
```

There are no wildcard arguments, arbitrary world paths, shell commands, or general sudo rights. Configuration and selected-world state are root-owned. A lock at `/run/civicrm-worldctl.lock` serializes lifecycle operations. Luanti itself continues to run as `ben`.

The updated lifecycle installer grants all eight exact commands to the developer account. The API identity receives the six legacy grants during base setup; its private-game upgrade adds `instance` and `reconcile_instances`. Use the JSON worker for a v2 instance request; invoking `instance` without a valid stdin body fails. Private-copy IDs cannot bypass approval through legacy start/switch commands.

Start and switch operations wait for startup and verify the selected world's process and bridge-loaded message from the current systemd journal invocation. A successful systemd start alone is not considered proof that the bridge loaded. Inspect the returned status if startup fails. Service logs, runtime files, lock files, state, and backups remain outside the Git repository.

For a legacy local test-world import (with that world already running; this is not an HTTPS account route):

```bash
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/01-create.json
```

## Verification

Run `python3 tests/run_lifecycle.py` on the Linux server to exercise two temporary worlds with real Luanti processes on a private loopback port. The suite checks world isolation, start/stop/switch behavior, failed-start rollback, locking, timeouts, and systemd readiness and transition handling. It leaves the development service untouched. Use `--mock-only` for the controller safety fixtures without starting Luanti. Fixtures live under the dedicated account’s home so they remain usable after access to the live world tree is restricted. The test driver and intentional broken-dependency mod are shared mods in an isolated user home, rather than permitted worldmods. Run `python3 tests/run_world_security.py` for real Linux ACL migration and rejection checks without controlling the service. After the privileged upgrade, verify as `codex_civicrmbridge` that writing `world.mt`, creating worldmods, and modifying receipts/results fail; inbox publication and result reads must still work.

## Rollback

These are legacy service rollback instructions. With private games configured, first quiesce portal dispatch and the bridge API scheduler; otherwise enabled schedules can start a game again. Preserve instance/receipt/configuration backups and do not treat rollback as save deletion.

To return to the legacy development world while reconciliation is quiesced, use `switch_world dev`. To restore the original service configuration entirely, run these commands as `ben` with sudo:

```bash
sudo systemctl stop luanti-dev.service
sudo rm /etc/systemd/system/luanti-dev.service.d/civicrm-worldctl.conf
sudo rm /etc/sudoers.d/civicrm-worldctl
sudo systemctl daemon-reload
sudo systemctl start luanti-dev.service
```

The original service unit was not edited, so removing this drop-in restores its original command targeting the development world. These commands retain both worlds and their data. Root-owned controller files, configuration, state, and backups can remain inert for inspection. For an upgrade rollback, restore the corresponding files from the recorded backup directory as root, retaining their original modes, then reload systemd; the backup manifest identifies files absent before installation.
