# Request and response examples

Start with the [integration contract](../docs/INTEGRATION.md). All passwords, identities and controller PIDs in these files are fictitious. Account response fixtures assume a fresh world with the shared mod enabled and the administrator configured as `ben`. An existing world can instead return `updated`, `unchanged` or a conflict. Never use these passwords for real participants.

## Runnable account jobs

Run the numbered root-level files in order. Each has its own ID. Their [expected results](expected/) describe the first run in a fresh world.

| Request | Action and precondition | Expected result |
| --- | --- | --- |
| [01-create.json](01-create.json) | Create admitted participant `demo_player` and facilitator `demo_facilitator` | [Two accounts created](expected/01-create.json) |
| [02-roles.json](02-roles.json) | Promote player and demote facilitator after 01 | [Both updated](expected/02-roles.json) |
| [03-withdraw.json](03-withdraw.json) | Withdraw player, retaining the facilitator role | [Updated; login denied](expected/03-withdraw.json) |
| [04-readmit.json](04-readmit.json) | Readmit player and demote to participant | [Updated; login allowed](expected/04-readmit.json) |
| [05-create-not-admitted.json](05-create-not-admitted.json) | Provision `demo_waitlisted` before allowing login | [Created; login denied](expected/05-create-not-admitted.json) |
| [06-preserve-password.json](06-preserve-password.json) | Supply another initial password for the existing player | [Unchanged; password ignored](expected/06-preserve-password.json) |
| [07-unchanged.json](07-unchanged.json) | New decision ID, already matching role/admission | [Unchanged](expected/07-unchanged.json) |
| [08-batch.json](08-batch.json) | Promote player, withdraw facilitator, readmit waitlisted account | [All three updated](expected/08-batch.json) |

In 03, withdrawal does not remove role privileges. In 04, the explicit participant role performs the demotion. In 06, the replacement example password never becomes the account's password; any password changed by the player remains intact. Updates omit the password unless demonstrating that rule.

For the dedicated test world, first select it explicitly:

```bash
sudo -n /usr/local/sbin/civicrm-worldctl switch_world test
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/01-create.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/02-roles.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/03-withdraw.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/04-readmit.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/05-create-not-admitted.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/06-preserve-password.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/07-unchanged.json
python3 tools/simulate.py --runtime-dir /home/ben/luanti-dev/worlds/test/civicrm_bridge examples/08-batch.json
sudo -n /usr/local/sbin/civicrm-worldctl switch_world dev
```

Switching disconnects connected players. These manual commands change the persistent test world, which may already contain these IDs. For reproducible results without touching either live world, run `python3 tests/run_examples.py` instead; it creates a disposable world and checks every account request against its fixture.

## Retries

Resubmit the exact `01-create.json` file with the original ID to demonstrate receipt replay. Its response is still [the original created result](expected/01-create.json), even after 08 changed these accounts. Their latest role/admission stays unchanged by the retry. Do not reserialize, change formatting or add fields when retrying.

Use `--job-id NEW_ID` for a new decision or corrected job. That is not a retry: it can apply state again. The [changed-content-same-id example](errors/requests/changed-content-same-id.json) deliberately demonstrates a conflict, not a password reset.

## Other example directories

- [errors/](errors/README.md): deliberately rejected account requests and partial-batch results. Some require raw publication because the simulator rejects invalid fields before delivery.
- [lifecycle/](lifecycle/README.md): implemented local worker JSON requests, their controller CLI equivalents, and actual controller response shapes. The worker adds correlation fields and durable receipts; the historical `proposed-requests/` path is retained.
- [expected/](expected/): complete account output JSON; never submit these files as requests.

Do not recursively submit all JSON files. Only the eight numbered root-level files are the successful runnable account sequence.
