# Lifecycle JSON worker

The local Linux worker implements the formerly proposed v1 lifecycle envelopes.
Run as the dedicated SSH account, with the existing restricted sudo grants:

```bash
cd /home/ben/.minetest/mods/civicrm_bridge
python3 tools/world_worker.py examples/lifecycle/proposed-requests/status.json
```

The `proposed-requests/` path is retained for existing links; these six files are
now accepted worker inputs. The root controller still accepts CLI arguments only.
Never publish these requests to a world's account inbox or use `tools/simulate.py`.
The optional [HTTPS API](../docs/HTTPS_API.md) wraps this worker with authentication,
durable acceptance and a background queue. Its installed service uses a separate
account and receipt directory. The CLI itself remains a single-request tool;
there is no CiviCRM extension.

Supply one UTF-8 JSON file, or `-` (the default) to read one request from stdin.
The maximum size is 16 KiB. Duplicate keys and unknown fields are rejected.
Required fields are integer `version: 1`, `job_id` (1–64 ASCII letters, digits,
underscores or hyphens), and `operation`. Allowed operations are `status`,
`stop_world`, `start_world`, and `switch_world`. Start/switch require `world_id`
equal to `dev` or `test`; status/stop forbid it. There are no caller-controlled
commands, paths, service names or configuration. The worker invokes fixed
`/usr/bin/sudo -n /usr/local/sbin/civicrm-worldctl` argument lists without a shell.
SSH authentication and the existing local account/sudo permissions authorize use.

The worker prints one JSON object: the controller response plus `version: 1` and
the request's `job_id`. Validation/input failures have `job_id: null`. All errors
have `ok: false` and `error.code`/`error.message`; details may be absent. Successful
outcomes exit 0; JSON failures exit 1; CLI usage errors exit 2. Controller errors,
including rollback details, are preserved. Non-JSON process errors return
`controller_process_error` with an exit code, without leaking raw stderr.

## Receipts and recovery

Receipts live outside Git and both worlds, by default in the account's private
`~/.local/state/civicrm-world-worker/receipts.sqlite3`. The directory must be owned
by the worker user and accessible only to that user. SQLite and the lock must be
private regular files, not symlinks. `--state-dir` is an operator option; keep one
stable directory per service. Do not alternate directories to retry jobs.

Requests are serialized with a nonblocking lock. Worker-level `busy` consumes no
ID, so retry the same bytes later. All dispatched outcomes, including controller
errors and controller-level `busy`, are recorded durably. Identical bytes with
the same ID replay the exact recorded response; changed bytes return
`job_id_conflict`. Receipt fingerprints include whitespace. A recorded error
needs a new ID for a new decision. Invalid input does not consume an ID.

A durable pending record is committed before command launch. If the worker
crashes between launch and recording a result, that ID returns `outcome_unknown`
and never automatically executes again. This avoids repeating a switch after
later decisions. It cannot provide an atomic transaction with systemd. A
600-second process timeout also returns a recorded `outcome_unknown`; the
privileged command may still be completing. `worker_state_error` can occur after
side effects if receipt persistence fails. For any uncertain outcome, inspect
fresh status and resolve it before sending a new mutating decision. Do not delete
pending records to force replay.

Status responses are receipts too: use a **fresh ID** for each current-state
query. A retry returns the old snapshot. Check `ready`, world identity, and
rollback fields before publishing dependent account jobs. Receipts have no
automatic expiry. Back up this private directory separately from Git; losing it
loses deduplication history. Live worlds and account queues remain separate.

## Verification

```bash
python3 tests/run_world_worker.py
python3 tests/run_lifecycle.py
```

Worker tests validate all six example inputs, strict rejection, fixed arguments,
durable replay/conflicts, pending-crash recovery, serialization, unsafe state,
controller errors/rollback, timeout and non-JSON outcomes. They do not invoke the
live controller. The lifecycle suite uses temporary worlds and loopback ports.
