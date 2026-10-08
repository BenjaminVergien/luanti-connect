# Integration contract for the future CiviCRM component

This describes the implemented prototype, not a public API or an implemented CiviCRM extension. See the [example catalog](../examples/README.md) for complete JSON files and expected results. All example accounts and passwords are fictitious.

## Available actions and transport

| Action | Current interface | JSON example |
| --- | --- | --- |
| Create participant and facilitator accounts | World-local `sync_accounts` inbox job | [01-create](../examples/01-create.json) |
| Promote or demote roles | Same job with existing username and desired role | [02-roles](../examples/02-roles.json) |
| Withdraw admission, disconnecting an online player | Same job with `admitted: false` | [03-withdraw](../examples/03-withdraw.json) |
| Readmit, optionally changing role at the same time | Same job with `admitted: true` | [04-readmit](../examples/04-readmit.json) |
| Create an account before allowing login | New account with `admitted: false` | [05-create-not-admitted](../examples/05-create-not-admitted.json) |
| Repeat a sync without resetting a password | Existing account; `initial_password` is ignored | [06-preserve-password](../examples/06-preserve-password.json) |
| Reconcile an account with no desired changes | Fresh job ID, same desired state | [07-unchanged](../examples/07-unchanged.json) |
| Apply several account decisions in one job | Multiple accounts, each with complete desired state | [08-batch](../examples/08-batch.json) |
| Retry a completed account job | Publish the identical bytes with the identical ID | Reuse [01-create](../examples/01-create.json), not a reserialized copy |
| Inspect current world/service state | `civicrm-worldctl status` returning JSON | [Lifecycle examples](../examples/lifecycle/README.md) |
| Stop the active world | `civicrm-worldctl stop_world` returning JSON | [Lifecycle examples](../examples/lifecycle/README.md) |
| Start configured world `dev` or `test` | `civicrm-worldctl start_world WORLD_ID` returning JSON | [Lifecycle examples](../examples/lifecycle/README.md) |
| Switch to configured world `dev` or `test` | `civicrm-worldctl switch_world WORLD_ID` returning JSON | [Lifecycle examples](../examples/lifecycle/README.md) |

Only `sync_accounts` is accepted as an inbox JSON operation. Lifecycle JSON is accepted by the [local worker](../tools/WORLD_WORKER.md), invoked as `python3 tools/world_worker.py REQUEST.json`, separately from the mod and simulator. The privileged controller takes command-line arguments. Do not send lifecycle requests to the account inbox.

## Account request v1

Encode UTF-8 JSON. Publish a complete file as `WORLD/civicrm_bridge/inbox/JOB_ID.json`. The job's `job_id` must match the filename without `.json`. The world is selected by the destination directory; there is no world-routing field in this JSON contract.

| Field | Required | Meaning and limits |
| --- | --- | --- |
| `version` | Yes | Numeric `1` |
| `job_id` | Yes | 1-64 ASCII letters, digits, underscores or hyphens |
| `operation` | Yes | Exactly `sync_accounts` |
| `accounts` | Yes | JSON array of 1-100 entries; no case-insensitive duplicate usernames |
| `accounts[].username` | Yes | Valid Luanti player name; stable, case-sensitive account identifier |
| `accounts[].role` | Yes | Exactly `participant` or `facilitator` |
| `accounts[].admitted` | Yes | JSON boolean `true` or `false`; not a string, number or null |
| `accounts[].initial_password` | For creation | Nonempty string, required when the account does not exist; ignored for existing managed accounts |

The entire file must be at most 256 KiB. An omitted optional password is allowed; an explicit null or empty password is rejected even when the account already exists. Every account entry must include both role and admission. Omitted accounts remain unchanged: a job is not a replacement of the entire roster. Fields outside this contract are not used by the current implementation; they do not add routing, authentication or CiviCRM identity handling.

Use `tools/simulate.py --runtime-dir WORLD/civicrm_bridge REQUEST.json` for valid local examples. A worker should write a temporary file in the target inbox, close it, then atomically publish the final filename without overwriting a pending job. Keep pending requests private: they can contain initial passwords. The mod ignores temporary names and processes one job per second in filename order, not submission order. Do not use unrelated random IDs to impose sequencing on dependent changes; wait for a result before submitting the next dependent job.

The server configuration administrator (`ben` in this setup) is protected. Existing accounts not owned by the bridge and case-conflicting names are rejected. The bridge cannot rename or take over accounts.

## Roles and admission

| Role | Privileges synchronized by the bridge |
| --- | --- |
| `participant` | `interact`, `shout` |
| `facilitator` | `interact`, `shout`, `fly`, `fast`, `teleport` |

Each sync grants or revokes these five privileges to match the role. Unrelated privileges are preserved. Admission is independent: a facilitator with `admitted: false` still has facilitator privileges but cannot log in. Withdrawal kicks online players and preserves passwords, accounts and player data. Readmission restores access without resetting passwords. Accounts created with admission disabled are provisioned but cannot join.

The bridge enforces an admission whitelist for everyone except the configured administrator. Unknown players cannot create accounts by attempting to log in. Missing or unusable admission state denies non-administrator admission.

## Account response v1

The complete JSON response is published atomically to `WORLD/civicrm_bridge/outbox/JOB_ID.json`. `tools/simulate.py` prints a shortened summary; a CiviCRM worker should read the complete outbox result, which includes `version`.

| Field | Meaning |
| --- | --- |
| `version` | Numeric `1` |
| `job_id` | Correlates with the input job and filename |
| `status` | `ok`: no account rejected; `partial`: some rejected; `error`: validation failed or all accounts rejected |
| `code` | Present for job-level validation, size or ID-conflict errors |
| `accounts` | Always an array; empty on job-level validation errors; otherwise one result per input account in input order |
| `accounts[].username` | Account identifier |
| `accounts[].status` | `created`, `updated`, `unchanged` or `rejected` |
| `accounts[].code` | Rejection reason; absent on success |

The complete request is validated before account changes begin. Valid batches can still partially succeed. They are not transactions; even a runtime/storage error can occur after some account changes. Do not assume an `error` response means every prior side effect was undone. Responses and receipts never contain passwords.

### Job-level error codes

| Code | Meaning |
| --- | --- |
| `invalid_job` | Malformed JSON, wrong version/operation, invalid or mismatched ID, or invalid array/count |
| `invalid_account` | Invalid account entry, missing/wrongly typed username/admission, or invalid supplied password |
| `invalid_username` | Luanti rejects the username |
| `unknown_role` | Role is not supported |
| `duplicate_username` | Two entries have the same username ignoring case |
| `oversized_job` | Input exceeds 256 KiB |
| `job_id_conflict` | This ID was already completed with different bytes |

Filenames with invalid IDs, or filenames not ending in `.json`, are ignored and produce no response. A valid filename with an invalid or mismatched payload `job_id` instead produces `invalid_job`. Storage failure or a corrupt checkpoint/receipt may halt imports and retain the input without a new result; treat a timeout as an unknown/pending outcome and investigate it.

### Per-account rejection codes

| Code | Meaning |
| --- | --- |
| `protected_account` | Configured administrator, including case variants |
| `existing_account` | Unmanaged existing account or case collision |
| `initial_password_required` | New account has no initial password |
| `storage_error` | Cannot persist bridge ownership/admission state |
| `account_creation_failed` | Account did not exist after attempted creation |
| `account_update_failed` | Authentication/privilege update raised an error |

The [error examples](../examples/errors/README.md) contain representative complete requests and responses, including a partial batch.

## Retry and ordering rules

Receipts are world-local and durable across process restarts. A completed ID refers to the exact raw request bytes, including whitespace. An identical retry returns the originally recorded response, without reapplying the action. A `created` replay therefore does not prove the account currently has the role/admission from that old job: later jobs may have changed it.

Assign a new ID for every new decision. Keep the exact bytes for retries; regenerating JSON with the same fields but different formatting can cause a conflict. A queued job must not be overwritten. Before reading a result for a reused ID, wait until the published input has been consumed; an earlier outbox result may still exist. The simulator already does this.

An oversized input permanently consumes its ID. Later oversized retries retain that rejection; an in-limit request with the same ID conflicts. Validation/rejection results are also completed outcomes, so a corrected request needs a fresh ID. Receipts/results have no automatic expiry policy in this prototype.

Jobs queued for a stopped world stay queued until that world starts. Switching worlds does not move or cancel queued jobs. There is no account-state query, queue-cancellation action or conditional revision check today. Serialize decisions per world/account to prevent an old queued decision overriding a newer decision.

## World lifecycle and future worker routing

Use the [lifecycle requests and controller response examples](../examples/lifecycle/README.md) with the [local worker](../tools/WORLD_WORKER.md). The worker maps permitted operations to fixed argument lists without a shell. `dev` and `test` are administrator-configured IDs. Arbitrary paths, game IDs and service names are not request parameters.

Current commands use the narrowly permitted `sudo -n /usr/local/sbin/civicrm-worldctl ...` wrapper. Luanti continues running as `ben`; the worker/developer account does not have general sudo. Controller output uses `ok` and state/error fields. The worker adds `version: 1` and `job_id`, with its own private durable receipts outside worlds and Git. Identical retries replay recorded results; changed bytes conflict. Interrupted pending jobs never automatically reexecute. Use fresh IDs for current status queries and inspect status after uncertain outcomes. Account receipts do not deduplicate world-control requests.

Only one world runs on the service/port. `status` reports the running world and selected world separately. `start_world` leaves the same ready world running, refuses another active world, and starts a stopped service. `stop_world` is idempotent and retains selection/data. `switch_world` validates the target before stopping, waits for readiness, and attempts to restore the prior world after failed target startup. Switching disconnects clients; they must reconnect. It does not migrate players or copy accounts.

Use `ready: true` rather than successful process creation alone. On failure inspect `error.code` and, for failed starts/switches, `error.details.rollback`: `restored`, `selection_restored`, or `failed`. A failed rollback needs operator attention; do not blindly retry or start another process. Concurrent lifecycle operations can return `busy`. Do not assume every lifecycle invocation completes within 30 seconds: stop/start and rollback can each have their own waits.

## Design decisions for the CiviCRM component

The local lifecycle worker exists; a CiviCRM component, account-delivery worker, and authenticated network transport still need implementation. The current mod trusts authorized local inbox writers and does not validate a shared password, signature, HTTP token or SSH identity inside JSON. Do not expose its inbox through an unauthenticated upload endpoint. Keep CiviCRM/GitHub/Hetzner credentials outside jobs, results and Git. A deployment key used to archive this code is separate from credentials used to submit bridge jobs.

Keep an external mapping from CiviCRM contact to stable Luanti username, scoped to the world. Display names and email changes should not rename Luanti accounts. Store CiviCRM contact/event/participation identifiers and audit/correlation metadata in the integration database; the mod does not store or return them. Avoid sending personal data that the mod does not need.

Resolve all relevant participation records to one desired role and one admission boolean before publishing. The mapping of CiviCRM statuses and roles is a policy decision for the future extension. If a person has several participations, cancellation of one must not automatically withdraw admission granted by another unless that is the chosen policy. Support explicit withdrawal updates; removing someone from the submitted array does nothing.

Persist the exact job bytes, ID, target world, pending/completed outcome and per-account results securely. Keep clear-text initial passwords only as long as needed for reliable creation delivery; existing-account updates should omit them. Losing retry bytes while a creation result is unknown needs an explicit recovery policy. A local CLI timeout is not proof that the server rejected or never received a request.

For dependent operations, start/switch the intended world, wait for readiness, publish its account job, and wait for its result. Account jobs cannot launch the server themselves. Lifecycle and account requests need separate dispatch paths and audit records even if a later worker offers one higher-level API.

## Not implemented

Password reset, account deletion/rename, arbitrary privilege grants, individual kick commands, roster/account querying, CiviCRM event membership storage, authenticated network delivery, provisioning Hetzner servers, and world creation/cloning/deletion are not available operations. The installer prepares the existing `dev` and a fresh `test` world; this is not a runtime world-creation API. Do not invent request names for these in the CiviCRM component until corresponding handlers and contracts exist.
