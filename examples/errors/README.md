# Account rejection and partial-success examples

These are legacy account v1 filesystem/mod fixtures. HTTPS validates requests before publication and can return HTTP/schema errors instead; they are not exact HTTPS error responses. For portal account v2 windows and private-instance routing, see the [integration contract](../../docs/INTEGRATION.md#account-request-v1v2).

These files are valid JSON but intentionally exercise errors. They are not part of the successful numbered sequence. Responses assume `name = ben`, the numbered examples have already run, and the specific new usernames here do not already exist.

| Request | Preconditions / reason | Expected response |
| --- | --- | --- |
| [protected-account](requests/protected-account.json) | `ben` is the configured administrator | [protected_account](responses/protected-account.json) |
| [missing-password](requests/missing-password.json) | `demo_no_password` is new, with no password | [initial_password_required](responses/missing-password.json) |
| [unknown-role](requests/unknown-role.json) | `owner` is not a supported role | [unknown_role; empty accounts](responses/unknown-role.json) |
| [invalid-admission](requests/invalid-admission.json) | String `"true"` is not a JSON boolean | [invalid_account; empty accounts](responses/invalid-admission.json) |
| [duplicate-username](requests/duplicate-username.json) | Same name twice, ignoring case | [duplicate_username; empty accounts](responses/duplicate-username.json) |
| [changed-content-same-id](requests/changed-content-same-id.json) | Reuses completed 01 ID with different content | [job_id_conflict](responses/changed-content-same-id.json) |
| [case-collision](requests/case-collision.json) | `demo_player` already exists with different case | [existing_account](responses/case-collision.json) |
| [partial-batch](requests/partial-batch.json) | `local_account` must already exist outside bridge ownership; `demo_partial` must be new | [One rejected, one created](responses/partial-batch.json) |

Unknown-role and invalid-admission requests are rejected by `tools/simulate.py` before they reach the server. Their fixtures describe what the mod returns if an authorized writer publishes those raw bytes. Whole-job validation errors change no accounts. A valid partial batch can create one account even while rejecting another; it must not be treated as an all-or-nothing transaction.

For a controlled raw validation example, with `test` already running, publish atomically using the simulator's publication helper while deliberately skipping its client-side validator:

```bash
python3 - <<'PY'
import json
from pathlib import Path
from tools.simulate import publish, wait_for_result

source = Path('examples/errors/requests/unknown-role.json')
raw = source.read_bytes()
job_id = json.loads(raw)['job_id']
runtime = Path('/home/ben/luanti-dev/worlds/test/civicrm_bridge')
queued = publish(runtime / 'inbox', job_id, raw)
result = wait_for_result(queued, runtime / 'outbox' / (job_id + '.json'), 15)
print(json.dumps(result, indent=2))
PY
```

Do not use this bypass for real submissions. Corrected payloads need fresh job IDs because rejected jobs also receive durable receipts. A timeout leaves the input queued and is not proof of rejection.

`tests/run_examples.py` sets up the unmanaged-account precondition in a disposable world and checks all these results. It does not create `local_account` in the live worlds. The main integration tests separately cover malformed JSON, oversized inputs and fail-closed handling of a corrupt receipt. They do not inject disk-write failures. See the [complete error-code tables](../../docs/INTEGRATION.md) for the other possible runtime outcomes.
