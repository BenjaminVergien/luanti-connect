# Development world lifecycle

This controller selects `dev` or `test` for the existing `luanti-dev.service`. There is one Luanti process and one public UDP port, 30000. Switching worlds gracefully stops the current world before starting the selected one. Players disconnect when the service stops.

| ID | World directory |
| --- | --- |
| `dev` | `/home/ben/luanti-dev/world` |
| `test` | `/home/ben/luanti-dev/worlds/test` |

Both worlds load the shared mod at `/home/ben/.minetest/mods/civicrm_bridge`. Each world has separate accounts, player data, bridge admission state, receipts, inbox, and outbox. The test world is created fresh; no development accounts or other world data are copied. The prototype JSON import interface remains unchanged. Pass the selected world's runtime directory to `tools/simulate.py` when importing into `test`.

## One-time installation

Review the lifecycle scripts, then run this from the existing `ben` SSH session:

```bash
sudo /usr/bin/python3 -I /home/ben/.minetest/mods/civicrm_bridge/lifecycle/install.py
```

The installer verifies that the existing service runs as `ben` and targets the development world, creates a fresh test world if needed, and prepares private runtime directories for `ben` and `codex_civicrmbridge`. Existing directories owned by either account retain their ownership; new directories belong to `ben`. Preparation uses held directory descriptors and rejects symlinks to keep permission changes inside the intended tree. It installs reviewed copies of the controller under `/usr/local/lib/civicrm-worldctl`, a wrapper at `/usr/local/sbin/civicrm-worldctl`, configuration under `/etc/civicrm-worldctl`, selected-world state under `/var/lib/civicrm-worldctl`, and a systemd drop-in. These installed files are root-owned; editing the Git checkout does not change privileged controller code.

The installer validates sudoers before activation, reloads systemd, and verifies the resulting service configuration. It does **not** restart the running service. The first installation selects `dev`; reinstallations retain the selected world. Unrecognized files or locally modified managed files cause installation to stop rather than overwrite them. Recognized upgrades retain backups under `/var/lib/civicrm-worldctl/backup-*` and restore managed files if verification fails.

## Commands

The dedicated developer account receives passwordless sudo access only to these exact commands:

```bash
sudo -n /usr/local/sbin/civicrm-worldctl status
sudo -n /usr/local/sbin/civicrm-worldctl stop_world
sudo -n /usr/local/sbin/civicrm-worldctl start_world dev
sudo -n /usr/local/sbin/civicrm-worldctl start_world test
sudo -n /usr/local/sbin/civicrm-worldctl switch_world dev
sudo -n /usr/local/sbin/civicrm-worldctl switch_world test
```

There are no wildcard arguments, arbitrary world paths, shell commands, or general sudo rights. Configuration and selected-world state are root-owned. A lock at `/run/civicrm-worldctl.lock` serializes lifecycle operations. Luanti itself continues to run as `ben`.

Start and switch operations wait for startup and verify the selected world's process and bridge-loaded message from the current systemd journal invocation. A successful systemd start alone is not considered proof that the bridge loaded. Inspect the returned status if startup fails. Service logs, runtime files, lock files, state, and backups remain outside the Git repository.

For a test-world import:

```bash
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/01-create.json
```

## Verification

Run `python3 tests/run_lifecycle.py` on the Linux server to exercise two temporary worlds with real Luanti processes on a private loopback port. The suite checks world isolation, start/stop/switch behavior, failed-start rollback, locking, timeouts, and systemd readiness and transition handling. It leaves the development service untouched. Use `--mock-only` for the controller safety fixtures without starting Luanti.

## Rollback

To return to development, use `switch_world dev`. To restore the original service configuration entirely, run these commands as `ben` with sudo:

```bash
sudo systemctl stop luanti-dev.service
sudo rm /etc/systemd/system/luanti-dev.service.d/civicrm-worldctl.conf
sudo rm /etc/sudoers.d/civicrm-worldctl
sudo systemctl daemon-reload
sudo systemctl start luanti-dev.service
```

The original service unit was not edited, so removing this drop-in restores its original command targeting the development world. These commands retain both worlds and their data. Root-owned controller files, configuration, state, and backups can remain inert for inspection. For an upgrade rollback, restore the corresponding files from the recorded backup directory as root, retaining their original modes, then reload systemd; the backup manifest identifies files absent before installation.
