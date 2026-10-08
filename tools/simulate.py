#!/usr/bin/env python3
"""Submit a fictitious account sync job to a local bridge inbox."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid


MAX_BYTES = 256 * 1024
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def load_job(path, job_id):
    with path.open("rb") as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("job exceeds 256 KiB")
    try:
        job = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError("job is not valid JSON") from None
    if not isinstance(job, dict):
        raise ValueError("job must be an object")
    if job_id is not None:
        job["job_id"] = job_id
    identifier = job.get("job_id")
    if not isinstance(identifier, str) or not JOB_ID.fullmatch(identifier):
        raise ValueError("job_id must contain 1–64 ASCII letters, digits, underscores, or hyphens")
    if type(job.get("version")) is not int or job["version"] != 1:
        raise ValueError("version must be 1")
    if job.get("operation") != "sync_accounts":
        raise ValueError("operation must be sync_accounts")
    accounts = job.get("accounts")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 100:
        raise ValueError("accounts must contain 1–100 entries")
    for index, account in enumerate(accounts, 1):
        if not isinstance(account, dict):
            raise ValueError(f"account {index} must be an object")
        if not isinstance(account.get("username"), str) or not account["username"]:
            raise ValueError(f"account {index} requires a username")
        if account.get("role") not in ("participant", "facilitator"):
            raise ValueError(f"account {index} has an invalid role")
        if type(account.get("admitted")) is not bool:
            raise ValueError(f"account {index} admitted must be a boolean")
        if "initial_password" in account and not isinstance(account["initial_password"], str):
            raise ValueError(f"account {index} initial_password must be a string")
    payload = (json.dumps(job, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise ValueError("serialized job exceeds 256 KiB")
    return identifier, payload


def publish(inbox, identifier, payload):
    destination = inbox / (identifier + ".json")
    temporary = inbox / ("." + identifier + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        # Same-filesystem hard link publishes the complete file atomically and
        # refuses to overwrite a pending job with the same identifier.
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def wait_for_result(inbox_file, outbox_file, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # The server records a result before consuming input. Waiting for the
        # input to disappear prevents reading an old result for a reused ID.
        if not inbox_file.exists() and outbox_file.is_file():
            with outbox_file.open("r", encoding="utf-8") as source:
                return json.load(source)
        time.sleep(0.2)
    raise TimeoutError("timed out; the queued job was left for the server to process")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jobfile", type=Path)
    parser.add_argument("--runtime-dir", type=Path,
                        default=Path("/home/ben/luanti-dev/world/civicrm_bridge"))
    parser.add_argument("--timeout", type=float, default=15.0, help="result wait in seconds (default: 15)")
    parser.add_argument("--job-id", help="override the input job_id")
    arguments = parser.parse_args()
    if not 0 < arguments.timeout < float("inf"):
        parser.error("--timeout must be finite and positive")
    try:
        identifier, payload = load_job(arguments.jobfile, arguments.job_id)
        inbox = arguments.runtime_dir / "inbox"
        outbox = arguments.runtime_dir / "outbox"
        if not inbox.is_dir() or not outbox.is_dir():
            raise ValueError("runtime inbox and outbox directories must already exist")
        queued = publish(inbox, identifier, payload)
        print(f"Submitted {identifier}; waiting for result.", flush=True)
        result = wait_for_result(queued, outbox / (identifier + ".json"), arguments.timeout)
        if not isinstance(result, dict) or result.get("job_id") != identifier:
            raise ValueError("result has an unexpected format or job_id")
        # Only display result fields; never echo the input or passwords.
        summary = {key: result[key] for key in ("job_id", "status", "code") if key in result}
        summary["accounts"] = [
            {key: account[key] for key in ("username", "status", "code") if key in account}
            for account in (result.get("accounts") or []) if isinstance(account, dict)
        ]
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ok" else 1
    except FileExistsError:
        print("error: a job with this ID is already pending; no file was overwritten", file=sys.stderr)
        return 2
    except TimeoutError as error:
        print(f"error: {error}", file=sys.stderr)
        return 3
    except (OSError, ValueError) as error:
        # JSON decode diagnostics and submitted content must never be printed.
        message = "result is not valid JSON" if isinstance(error, json.JSONDecodeError) else str(error)
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
