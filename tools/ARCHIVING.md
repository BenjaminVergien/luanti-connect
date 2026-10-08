# GitHub code archive

The archive is the private repository `Stewie23/civicrm_bridge`. Preserve commit history with ordinary pushes to `main`; do not force-push or mirror unrelated refs. Only code, documentation, tests and fictitious examples belong in it. World accounts, passwords, configuration, databases and bridge runtime files remain outside the checkout.

The development account uses a separate repository-specific write deploy key. Its private file and pinned GitHub host key live under `/home/codex_civicrmbridge/.ssh`, outside Git. The SSH command is configured only in this checkout's Git configuration. It disables agent forwarding and other identities, requires the pinned host key, and uses no password or personal access token. Never display or copy the private key. Revoke this deploy key in GitHub when retiring the development server. Provisioned runtime servers should use separate read-only access.

Install Gitleaks from its official release, verify the release checksum, and make its binary available on `PATH` or at `~/.local/bin/gitleaks`. In each Linux checkout enable the versioned hook:

```bash
git config --local core.hooksPath tools/git-hooks
chmod +x tools/git-hooks/pre-push
python3 -I tools/check_archive.py
git push -u origin main
```

The pre-push check reads every pending update from Git and audits all local-ref history plus the full history of each outgoing commit, including explicit pushes of commits with no local branch or tag. It deliberately does not subtract remote history. Both the sensitive-path check and Gitleaks use the same frozen commit list; the scanner receives bounded batches with `--no-walk`. Annotated tags must peel to commits; direct tree/blob pushes and tags targeting non-commits are rejected. Deletions add no outgoing commits. Malformed records, missing objects and shallow checkouts block the push. Running the checker without `--pre-push` retains the standalone local-history audit. Missing tools or failed scans block the push, and scanner matches are not printed. `.gitignore` helps prevent accidental staging but cannot remove already committed files. Neither mechanism guarantees detection of every secret; review commits before archiving and keep credentials outside the checkout.

For a suspected finding, investigate locally with `gitleaks git --redact=100 --log-opts=--all .`. Do not paste raw matches into chat. If a real credential was committed, rotate or revoke it and remove it from the outbound history before the first push. Do not bypass the hook to publish a finding.

Run `python3 tests/run_archive.py` for disposable local-remote push fixtures, including dangling prohibited files and fictitious secret markers. No live service or GitHub refs are changed by these tests.

The hook does not install automatically in fresh clones. Enable it for each contributor checkout. Local hooks can be bypassed or edited, so configure receiving-side secret protection and required repository checks before relying on this as a shared security boundary. GitHub stores the code and its history; backups of live worlds are a separate task.
