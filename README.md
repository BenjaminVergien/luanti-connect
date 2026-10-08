# CiviCRM Bridge

See [GitHub code archiving](tools/ARCHIVING.md) for repository credentials and checks before pushing.

A small Luanti mod for local JSON account imports. This first version uses fictitious participants, the existing Luanti authentication API, and a filesystem inbox. It creates accounts, synchronizes roles and admission, and writes JSON results while the server runs. It does not connect to CiviCRM yet.

## Installation

For the restricted `dev`/`test` service controller, see [world lifecycle setup](lifecycle/README.md).

The development setup uses Luanti 5.17.0 with LuaJIT and Minetest Game:

- Shared mod and standalone Git repository: `/home/ben/.minetest/mods/civicrm_bridge`
- World: `/home/ben/luanti-dev/world`
- Server configuration: `/home/ben/luanti-dev/server.conf`
- Runtime inbox and outbox: `/home/ben/luanti-dev/world/civicrm_bridge`
- Service: `luanti-dev.service`, running as `ben`; client UDP port 30000.

Enable `load_mod_civicrm_bridge = mods/civicrm_bridge` in each selected world's `world.mt`. The shared mod directory belongs to the operating-system user running Luanti (`ben` here); other service users need their own installation or a configured shared content path. Keep one source copy: do not also place this mod in a world's `worldmods` directory.

Each world keeps its own authentication records, bridge mod storage, and runtime inbox/outbox. The mod derives its runtime directory from `core.get_worldpath()`; worlds do not share account or admission state. Precreate each runtime directory and its `inbox`, `outbox`, and `receipts` children with inherited read/write access for the service user and the intended integration/developer account. Both accounts need directory traversal and file creation/deletion access. Keep runtime files accessible only to the intended local accounts: pending inputs contain initial passwords.

From the existing **ben** session, load the mod with:

```bash
sudo systemctl restart luanti-dev.service
```

The developer account needs no sudo rights. Later imports require no restart. This mod enables a strict admission whitelist: existing accounts other than the configured administrator need a bridge-managed admission record before they can join. Do not enable it on a different world without accounting for that behavior.

## Local demonstration

From the mod directory, run these jobs in order with Python 3:

```bash
python3 tools/simulate.py examples/01-create.json
python3 tools/simulate.py examples/02-roles.json
python3 tools/simulate.py examples/03-withdraw.json
python3 tools/simulate.py examples/04-readmit.json
```

Every example identity and password is fictitious. Use these only in the development world. After the first job, `demo_player` can log in using the example password. Change that password in the client, then rerun the creation job with a fresh ID to verify that imports preserve the new password:

```bash
python3 tools/simulate.py --job-id demo-password-preserved examples/01-create.json
```

Keep the player connected during the role and withdrawal jobs to observe live privilege updates and immediate disconnection. The readmission job permits login again without changing the password or deleting player data.

The simulator accepts `--runtime-dir PATH`, `--timeout SECONDS` (default 15), and `--job-id ID`. Its default runtime directory is the development world shown above; use `--runtime-dir /path/to/world/civicrm_bridge` when targeting another world. It atomically publishes a complete input, refuses to overwrite a pending job, waits for the server to consume it, and prints result fields only. Exit codes are 0 for success, 1 for a partial/error result, 2 for input or filesystem errors, and 3 for a timeout. A timeout leaves the job queued; inspect the service and result file before retrying.

## JSON contract

```json
{
  "version": 1,
  "job_id": "demo-001",
  "operation": "sync_accounts",
  "accounts": [
    {
      "username": "demo_player",
      "initial_password": "DemoOnly-ChangeMe!",
      "role": "participant",
      "admitted": true
    }
  ]
}
```

- `job_id`: 1–64 ASCII letters, digits, underscores, or hyphens. Publish as `inbox/<job_id>.json`.
- `accounts`: 1–100 entries. The complete file must not exceed 256 KiB. Usernames identify accounts; omitted accounts remain unchanged.
- `initial_password`: a nonempty string required for account creation. For an existing bridge-managed account it is ignored, including on repeated imports.
- `role`: `participant` or `facilitator`; `admitted`: an explicit JSON boolean.

For manual delivery, write a temporary filename in the inbox and rename it to the final `.json` name only when complete. The simulator uses an atomic hard link to publish without overwriting another pending submission. Temporary files are ignored. The mod polls once per second and processes one job per poll in filename order.

The result appears atomically at `outbox/<job_id>.json`, with `job_id`, overall `status` (`ok`, `partial`, or `error`), and `accounts` containing `username` and `status` (`created`, `updated`, `unchanged`, or `rejected`), with an optional `code`. Invalid jobs can return an overall error without account outcomes. The complete structure is validated before any account changes; valid batches can still have individual account conflicts, so the batch is not a transaction.

Completed IDs are retained in durable receipts. Byte-identical retries return their recorded results without reapplying account changes. Reusing an ID for changed content, including whitespace changes, yields `job_id_conflict`; use a fresh ID for each new change. Oversized inputs permanently consume their IDs with an `oversized_job` result; oversized retries retain that rejection, and a later in-limit input with that ID conflicts. Input is removed only after a result is recorded. Results, receipts, and logs do not contain passwords, and input is not archived.

## Accounts, roles, and admission

| Role | Managed privileges |
| --- | --- |
| `participant` | `interact`, `shout` |
| `facilitator` | `interact`, `shout`, `fly`, `fast`, `teleport` |

Each import synchronizes these five privileges, including revocation on demotion. Unrelated manually assigned privileges remain unchanged; JSON cannot grant arbitrary privileges. Existing unmanaged accounts, case-conflicting usernames, and the configured administrator are rejected rather than taken over.

Admission is independent of privileges. Only admitted bridge accounts and the administrator configured by Luanti's `name` setting (`ben` here) may join. Unknown accounts cannot self-register through login. Missing admission state denies access. Withdrawal disconnects online players immediately and blocks reconnection, while retaining their accounts, passwords, and player data. Ownership and admission are persisted in an atomic, password-free runtime checkpoint, with a mod storage mirror; durable receipts retain completed job outcomes.

Password reset is deliberately separate and is not implemented in v1. Event memberships, CiviCRM integration, HTTP delivery, and automated SSH transfer are also outside v1.

## Tests and repository boundaries

Run the isolated engine integration checks on the server:

```bash
python3 tests/run_integration.py
```

The tests use a temporary world, an isolated user home with a shared mod installation, and a loopback-only port. Also check login, role updates, withdrawal, and readmission with a real client in the development world.

Version only this mod directory. Keep the surrounding world, authentication databases, mod storage, inbox, outbox, and real credentials out of Git. There is no Git remote or automatic push. To stop enforcing the bridge whitelist, disable the mod in `world.mt` and restart the service; account and privilege changes already applied remain in the world.

The existing mod directory remains owned by `ben`. From that directory, the dedicated SSH account can use `git -c safe.directory="$PWD" status` (and the same prefix for other Git commands). This trusts only this repository for that invocation and leaves global Git settings unchanged.
