# Portal and bridge integration contract

**Implemented:** Luanti Portal orchestrates private games and timed accounts through this bridge. The portal owns users, organizations, approvals, reservations and its roster; the API owns durable delivery, the controller manages copies/service transitions, and the mod applies account/admission state. The [HTTPS guide](HTTPS_API.md) owns endpoint, authentication and deployment details. All example identities and passwords are fictitious.

**Provisional / moving parts:** publication/upgrade procedures and host-specific paths/identities. Source and tests describe behavior, not the installed state of a remote server. **Planned:** a separate CiviCRM component; its open policy decisions are below.

## Current portal flow

Discover published templates with `GET /v1/world-templates`, returning an envelope with integer `version: 1` and a `templates` array. Each entry contains `template_id`, `name` and `revision`. IDs match `[A-Za-z0-9_-]{1,32}`, names are nonempty (up to 255 characters), and revisions are 64 lowercase hexadecimal characters identifying immutable SHA-256 bundles. An empty list is valid; retain the previous catalogue on fetch/validation failure. A template is not a playable instance. `/v1/worlds` is the separate legacy saved-world catalogue.

Approval reserves one non-overlapping period on a server and selects a template revision. The portal creates a private copy, configures its approved window and starts it when due; it submits account v2 jobs to that instance. Windows use UTC Unix seconds, inclusive `starts_at` and exclusive `ends_at`. Portal calendar dates cover whole days in `Europe/Berlin`, including clock changes. Date edits retain the previous approval until reapproved; an extension reuses the save.

Changing templates on the approved server prepares a new copy, archives the previous one and recreates the roster from the portal's stored initial passwords. Passwords changed inside the old game and player progress are not transferred. Expiry stops a copy while retaining its save. Archival permanently disables reactivation and retains the save too.

The HTTP route prefix stays `/v1/`; asynchronous envelopes use `version: 1`, while account/instance payloads and their results can use `version: 2`. See [instance lifecycle v2](#private-instance-lifecycle-v2) and [HTTPS response states](HTTPS_API.md#api-and-backend-usage).

## Account actions and legacy examples

The numbered fixtures below demonstrate account v1 through a local filesystem inbox. `tools/simulate.py` only accepts v1; it is not a portal v2 client. The account semantics carry over to v2, which adds a required window per account.

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

Only `sync_accounts` is accepted as an account-inbox operation. The portal submits it through `POST /v1/worlds/{instance_id}/accounts/jobs`; HTTPS account routing accepts registered private instances, not templates or legacy `dev`/`test` saves. Lifecycle JSON goes to the separate `/v1/lifecycle/jobs` endpoint or [local worker](../tools/WORLD_WORKER.md). The privileged wrapper uses fixed CLI operations and, for instance v2, validated JSON on stdin. Never send lifecycle requests to an account inbox.

## Account request v1/v2

Encode UTF-8 JSON. Publish a complete file as `WORLD/civicrm_bridge/inbox/JOB_ID.json`. The job's `job_id` must match the filename without `.json`. The world is selected by the destination directory; there is no world-routing field in this JSON contract.

| Field | Required | Meaning and limits |
| --- | --- | --- |
| `version` | Yes | Integer `1` (legacy) or `2` (portal) |
| `job_id` | Yes | 1-64 ASCII letters, digits, underscores or hyphens |
| `operation` | Yes | Exactly `sync_accounts` |
| `accounts` | Yes | JSON array of 1-100 entries; no case-insensitive duplicate usernames |
| `accounts[].username` | Yes | Stable, case-sensitive account identifier; HTTPS requires `[A-Za-z0-9_-]{1,20}` |
| `accounts[].role` | Yes | Exactly `participant` or `facilitator` |
| `accounts[].admitted` | Yes | JSON boolean `true` or `false`; not a string, number or null |
| `accounts[].access_window` | v2 only | Exactly `{starts_at, ends_at}`; integer Unix seconds with `starts_at < ends_at`, absolute values at most `2^53 - 1` |
| `accounts[].initial_password` | For creation | Nonempty string, required when the account does not exist; ignored for existing managed accounts |

The entire file must be at most 256 KiB, with 1-100 accounts per job. An omitted optional password is allowed; an explicit null or empty password is rejected even for an existing account. Every account needs role and admission. v2 additionally requires a window per account; v1 forbids a supplied window and preserves a previously stored window on an existing managed account. Omitted accounts remain unchanged. HTTPS rejects unknown fields, duplicate keys and non-finite numbers; the local mod is not a network authentication boundary.

Example portal payload (fictitious timestamps/password; routing selects the registered instance):

```json
{"version":2,"job_id":"portal-accounts-001","operation":"sync_accounts","accounts":[{"username":"demo_player","initial_password":"DemoOnly-ChangeMe!","role":"participant","admitted":true,"access_window":{"starts_at":1800000000,"ends_at":1800003600}}]}
```

Use `tools/simulate.py --runtime-dir WORLD/civicrm_bridge REQUEST.json` for the local v1 examples. A filesystem writer should write a temporary file in the target inbox, close it, then atomically publish the final filename without overwriting pending input. Keep pending requests private: they can contain initial passwords. The mod ignores temporary names and processes one job per second in filename order, not submission order. Wait for results before submitting dependent changes.

The server configuration administrator (`ben` in this setup) is protected. Existing accounts not owned by the bridge and case-conflicting names are rejected. The bridge cannot rename or take over accounts.

## Roles and admission

| Role | Privileges synchronized by the bridge |
| --- | --- |
| `participant` | `interact`, `shout` |
| `facilitator` | `interact`, `shout`, `fly`, `fast`, `teleport` |

Each sync grants or revokes these five privileges to match the role. Unrelated privileges are preserved. Admission is independent: a facilitator with `admitted: false` still has facilitator privileges but cannot log in. Withdrawal kicks online players and preserves passwords, accounts and player data. Readmission restores access without resetting passwords. Accounts created with admission disabled are provisioned but cannot join.

For timed accounts, `admitted: true` permits login only while `starts_at <= now < ends_at`. The mod checks admission on login and disconnects players outside the stored window while running; enforcement does not depend on a last-minute portal withdrawal. The API/controller must also be running to reconcile game service schedules, and host clocks must be correct.

The bridge enforces an admission whitelist for everyone except the configured administrator. Unknown players cannot create accounts by attempting to log in. Missing or unusable admission state denies non-administrator admission.

## Account response v1/v2

The mod atomically publishes the response to `WORLD/civicrm_bridge/outbox/JOB_ID.json`. The simulator prints a shortened summary; HTTPS polling wraps the complete account result in the v1 job envelope.

| Field | Meaning |
| --- | --- |
| `version` | `1` or `2`, matching the decoded request version; unparseable/oversized input can return a v1 error |
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
| `invalid_access_window` | Missing/invalid v2 window or a window supplied in v1 |
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

An oversized filesystem input permanently consumes its ID. Later oversized retries retain that rejection; an in-limit request with the same ID conflicts. Mod validation/rejection results are also completed outcomes, so a corrected request needs a fresh ID. HTTPS rejects oversized/invalid bodies before durable job acceptance instead. Receipts/results have no automatic expiry policy.

Jobs queued for a stopped world stay queued until it runs. Switching worlds does not move queued jobs. There is no bridge account-state query, queue-cancellation action or conditional account revision check. Serialize decisions per instance/account. The portal tracks submission before delivery, cancels superseded unsent work, preserves already submitted jobs, and waits for revocation before new grants. Unknown outcomes block dependent grants until resolved. This does not add a bridge queue-cancellation API.

## Private-instance lifecycle v2

Every request contains `version: 2`, `job_id`, `operation` and `instance_id`. Job IDs use the `job_id` syntax above; instance IDs match `g[0-9a-z]{26}`. The caller chooses and persists the instance identity before creation; the portal uses a lower-case ULID prefixed with `g`. Requests are at most 16 KiB; duplicate keys and unknown fields are rejected.

| Operation | Additional required fields / behavior |
| --- | --- |
| `create_instance` | `template_id`, `template_revision`, `access_window`; creates a clean private save, initially disabled/stopped |
| `configure_instance` | `access_window`, integer `generation` (1 to `2^53 - 1`), boolean `enabled`; applies the approved schedule |
| `start_instance` | None; starts only within an enabled approved window |
| `stop_instance` | None; stops this instance without deleting it |
| `instance_status` | None; inspects this instance |
| `archive_instance` | None; permanently disables/stops the instance and retains its save |

Template IDs/revisions and access windows follow the formats above. Reconfiguration requires an increasing generation for changed settings; the same generation with identical settings is idempotent. Reusing a creation identity for different origin fields conflicts. Keep the approved immutable revision available for interrupted creation.

Normal instance results include `version: 2`, `job_id`, `instance_id`, `ok`, and on success `state` (`stopped`, `starting`, `running` or `archived`). Errors include `error.code`; worker/recovery failures can retain a v1 error shape. A completed job with `ok: false` is not success. Account batches use their separate `status`/per-account outcomes. Polling an old job replays its historical result, so use a fresh status job for current state.

One game runs on the service/port. Overlapping enabled windows are rejected. With private-game configuration active, the API calls controller reconciliation approximately every ten seconds, including after restart. An enabled due game may be restarted after `stop_instance`; disable its schedule to keep it stopped. Stopping/archiving an inactive copy does not stop a different active game.

See [HTTPS setup/publication](HTTPS_API.md) for clean-bundle rules, retained revision mappings and installed-snapshot upgrades. Publishing a template is an operator action, not a caller-supplied filesystem path or a public provisioning endpoint.

## Legacy lifecycle v1: operator maintenance

Use the [lifecycle requests and controller response examples](../examples/lifecycle/README.md) with the [local worker](../tools/WORLD_WORKER.md). The worker maps permitted operations to fixed argument lists without a shell. `dev` and `test` are administrator-configured IDs. Arbitrary paths, game IDs and service names are not request parameters.

These v1 commands use the narrowly permitted `sudo -n /usr/local/sbin/civicrm-worldctl ...` wrapper. Luanti runs as `ben` in the recipe; the worker/developer account has no general sudo. The worker adds `version: 1` and `job_id`, with separate private receipts. Identical retries replay results; changed bytes conflict. Interrupted v1 jobs do not automatically reexecute. Interrupted v2 instance jobs can resume through identity/generation checks in the controller; a recorded terminal result still replays. Use fresh IDs for current status queries and inspect uncertain outcomes. Account receipts do not deduplicate lifecycle requests.

Only one world runs on the service/port. `status` reports the running world and selected world separately. `start_world` leaves the same ready world running, refuses another active world, and starts a stopped service. `stop_world` is idempotent and retains selection/data. `switch_world` validates the target before stopping, waits for readiness, and attempts to restore the prior world after failed target startup. Switching disconnects clients; they must reconnect. It does not migrate players or copy accounts.

Use `ready: true` rather than successful process creation alone. On failure inspect `error.code` and, for failed starts/switches, `error.details.rollback`: `restored`, `selection_restored`, or `failed`. A failed rollback needs operator attention; do not blindly retry or start another process. Concurrent lifecycle operations can return `busy`. Do not assume every lifecycle invocation completes within 30 seconds: stop/start and rollback can each have their own waits.

## Planned CiviCRM component

**Planned, not implemented:** a CiviCRM component using the existing authenticated transport. The policies below are design considerations, not current portal requirements or a delivery commitment. The mod trusts authorized local inbox writers; it does not authenticate network credentials inside JSON. Keep CiviCRM/GitHub/Hetzner credentials outside jobs, results and Git; archive deploy keys and API bearer tokens have different purposes.

Open decisions for that component:

- Map contacts to stable instance-scoped Luanti usernames; retain contact/event/participation and audit identifiers in the integration database, not in mod jobs.
- Resolve multiple participations to one role/admission/window policy, including explicit withdrawal. Omitting an account does not withdraw it.
- Choose private credential retention and uncertain-outcome recovery policies while persisting exact bytes, IDs, targets and per-account outcomes for retries.
- Prepare/configure a private instance, wait for it to run within approval, then submit accounts. Account jobs cannot launch a server; lifecycle and account outcomes remain separate.

## Known limitations

The bridge offers no password reset, account deletion/rename, arbitrary privilege grants, individual kick command, roster/account query, CiviCRM event membership storage, Hetzner provisioning or save-deletion operation. The separate portal implements Hetzner VM/engine provisioning; it does not automatically deploy this bridge or game content. Private-copy creation from clean published templates is implemented; arbitrary world paths and saved-world cloning are unsupported. Archived saves and completed receipts have no automatic expiry policy. Unknown outcomes may require operator recovery; this contract does not imply live deployment validation.
