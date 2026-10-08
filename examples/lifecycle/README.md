# World-control examples

**The files in `proposed-requests/` are now implemented input for the local JSON worker.** The directory name is retained for existing links. Run `python3 tools/world_worker.py examples/lifecycle/proposed-requests/status.json` from the repository. See the [worker contract and recovery rules](../../tools/WORLD_WORKER.md). Do not publish lifecycle requests in the bridge inbox or pass them to `tools/simulate.py`. The privileged controller still takes the exact CLI arguments below. Response files show its output without the worker's added `version` and `job_id`; PIDs are illustrative.

## Actions

| Worker input | Controller CLI | Typical controller response |
| --- | --- | --- |
| [status](proposed-requests/status.json) | `sudo -n /usr/local/sbin/civicrm-worldctl status` | [running dev](responses/status-running-dev.json) |
| [stop-world](proposed-requests/stop-world.json) | `sudo -n /usr/local/sbin/civicrm-worldctl stop_world` | [stopped](responses/stop-world.json) |
| [start-dev](proposed-requests/start-dev.json) | `sudo -n /usr/local/sbin/civicrm-worldctl start_world dev` | [started dev](responses/start-dev.json) |
| [start-test](proposed-requests/start-test.json) | `sudo -n /usr/local/sbin/civicrm-worldctl start_world test` | [started test](responses/start-test.json) |
| [switch-dev](proposed-requests/switch-dev.json) | `sudo -n /usr/local/sbin/civicrm-worldctl switch_world dev` | [switched to dev](responses/switch-dev.json) |
| [switch-test](proposed-requests/switch-test.json) | `sudo -n /usr/local/sbin/civicrm-worldctl switch_world test` | [switched to test](responses/switch-test.json) |

The envelope contains integer `version: 1`, a correlation `job_id`, `operation`, and `world_id` for start/switch. The worker strictly validates fields, maps them to fixed argument lists, and records durable receipts. Existing SSH/account and exact sudo grants authorize invocation. `status` and `stop_world` act on the one service and forbid a world ID; stop does not accept a target world parameter.

`dev` and `test` are currently the only allowed IDs. The installed root-owned configuration maps IDs to paths. This permits selecting existing worlds, not creating them. See [installation and rollback](../../lifecycle/README.md).

## Successful outputs

| Field | Meaning |
| --- | --- |
| `ok` | Boolean outcome of the controller command |
| `state` | `running`, `starting`, `stopped` or `unmanaged` |
| `world_id` | Observed configured running/transitioning world, or null when stopped/unmanaged |
| `selected_world_id` | Persistent selection used for the next service start |
| `active` | Service/process is active or transitioning; does not imply readiness |
| `ready` | Observed configured process has emitted both bridge-ready and server-listening messages in the current invocation |
| `pid` | Observed service main PID; zero when no process exists |
| `operation` | Present on start, stop and switch results; absent on `status` |
| `changed` | Whether this command performed a start/stop/switch; absent on `status` |

All four status forms are illustrated: [running](responses/status-running-dev.json), [starting](responses/status-starting-test.json), [stopped](responses/status-stopped.json), [unmanaged](responses/status-unmanaged.json). A transitioning service can have `active: true` and `pid: 0`. An unmanaged active process is not stopped or replaced by the controller.

No-op examples: [start the same world](responses/start-same-world.json), [switch to the same world](responses/switch-same-world.json), and [stop an already stopped service](responses/stop-already-stopped.json). A ready same-world command does not restart it. Starting a different active world returns [already_running](responses/already-running.json); use switch instead. Direct CLI commands inspect current state. The JSON worker replays completed receipts for identical IDs/bytes; use a fresh ID for a new decision or current status query.

## Errors and recovery

An implemented controller error has `ok: false` and an `error` object containing `code`, `message`, and optional `details`. The generic `configuration_error` branch omits `details`; treat its absence as an empty object. Do not branch on the prose message. Normal controller success exits 0 and errors exit 1. Argument parsing errors exit 2; sudo denial and command-launch failures may return plain stderr rather than JSON, so the future worker must handle both process errors and JSON outcomes.

| Code | Worker response |
| --- | --- |
| `busy` | Another lifecycle operation holds the lock; serialize commands and retry later |
| `already_running` | Another configured world is active; make an explicit switch decision |
| `unknown_world`, `invalid_world` | Invalid ID or unusable target; repair configuration/input before retrying |
| `unmanaged_instance` | Foreign executable or unconfigured world; operator investigation |
| `stop_timeout`, `startup_timeout`, `service_error` | Operation/state uncertainty; inspect status before another decision |
| `start_failed`, `switch_failed` | Inspect rollback details and status |
| `permission_denied`, `configuration_error`, `invalid_configuration`, `unsafe_configuration`, `unsafe_state`, `invalid_state`, `lock_error` | Installation/state/permissions need operator attention |

Unknown IDs are also excluded by the exact sudo command grant. The worker rejects IDs other than `dev` and `test` before invocation.

Examples include [busy](responses/busy.json), [failed switch with prior world restored](responses/switch-failed-restored.json), [failed start with stopped selection restored](responses/start-failed-selection-restored.json), and [failed rollback](responses/switch-failed-rollback-failed.json). `details.cause` gives the underlying failure code. `details.previous_world_id` identifies the former running world or former stopped selection.

`rollback: restored` means the previously running world became ready again. `selection_restored` means it was previously stopped and remains stopped with its old selection. `failed` means recovery did not complete; do not assume any world is ready. Failures while stopping the original service can return directly without a rollback object. Preserve the error and inspect a fresh status.

Switching saves/stops the current world before starting the next; clients disconnect and must reconnect. Accounts and queued jobs stay in their own worlds. This is not a player transfer or an account-copy operation. Use readiness before publishing dependent account jobs. Actual controller CLI outputs contain neither `job_id` nor `version`; the JSON worker adds them and stores its own retry records.
