#!/usr/bin/env python3
"""Exercise the real Luanti engine in a disposable, loopback-only world.

No live-world state is read or modified. Run on the server, where the mod and
Minetest Game are installed. Requires only Python's standard library.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time


SECRETS = ["Initial-Fictional-Password!", "Player-Changed-Password!", "Ignored-Password!"]


class Harness:
    def __init__(self, engine, mod, base, game_path):
        self.path = Path(tempfile.mkdtemp(prefix="civicrm-bridge-test-", dir=base))
        self.mod = mod
        self.world = self.path / "world"
        self.world.mkdir()
        self.runtime = self.world / "civicrm_bridge"
        for name in ("inbox", "outbox"):
            (self.runtime / name).mkdir(parents=True)
        self.control = self.world / "test-control"
        self.control.mkdir()
        mods = self.world / "worldmods"
        mods.mkdir()
        driver = mods / "bridge_test_driver"
        driver.mkdir()
        (driver / "mod.conf").write_text("name = bridge_test_driver\ndepends = civicrm_bridge\n")
        shutil.copyfile(mod / "tests" / "driver.lua", driver / "init.lua")
        (self.world / "world.mt").write_text(
            "gameid = minetest\nbackend = sqlite3\nplayer_backend = sqlite3\n"
            "auth_backend = sqlite3\nmod_storage_backend = sqlite3\n"
            "load_mod_civicrm_bridge = mods/civicrm_bridge\nload_mod_bridge_test_driver = true\n"
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.config = self.path / "server.conf"
        self.config.write_text(
            "name = ben\nbind_address = 127.0.0.1\nport = %d\n"
            "server_announce = false\nenable_ipv6 = false\n"
            "creative_mode = false\nenable_damage = false\n" % port
        )
        self.command = [engine, "--server", "--world", str(self.world),
                        "--config", str(self.config), "--gameid", "minetest",
                        "--logfile", str(self.path / "engine.log")]
        self.environment = dict(os.environ)
        isolated_home = self.path / "home"
        games = isolated_home / ".minetest" / "games"
        games.mkdir(parents=True)
        shared_mods = isolated_home / ".minetest" / "mods"
        shared_mods.mkdir()
        (shared_mods / "civicrm_bridge").symlink_to(mod, target_is_directory=True)
        if game_path:
            # Accept either the game itself or the directory containing it.
            if not (game_path / "game.conf").is_file():
                game_path = game_path / "minetest_game"
            if not (game_path / "game.conf").is_file():
                raise ValueError("Minetest Game not found at %s" % game_path)
            (games / "minetest_game").symlink_to(game_path.resolve(), target_is_directory=True)
        self.environment["HOME"] = str(isolated_home)
        self.process = None
        self.output = None
        self.sequence = 0

    def start(self):
        self.output = (self.path / "process.log").open("ab")
        self.process = subprocess.Popen(self.command, env=self.environment,
                                        stdout=self.output, stderr=subprocess.STDOUT)
        readiness = self.control_command("ready")
        assert readiness["ready"]
        assert Path(readiness["bridge_modpath"]).resolve() == self.mod.resolve()
        assert not (self.world / "worldmods" / "civicrm_bridge").exists()

    def wait_json(self, path, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError("Luanti exited unexpectedly; inspect %s" % self.path)
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
        raise AssertionError("Timed out waiting for %s" % path)

    def atomic_write(self, path, body):
        temporary = path.with_suffix(".tmp")
        temporary.write_text(body)
        temporary.replace(path)

    def control_command(self, operation, **kwargs):
        self.sequence += 1
        source = self.control / ("command-%d.json" % self.sequence)
        result = self.control / ("response-%d.json" % self.sequence)
        self.atomic_write(source, json.dumps(dict(operation=operation, **kwargs)))
        value = self.wait_json(result)
        assert "error" not in value, "Test driver failed: %s" % value
        result.unlink()
        return value

    def snapshot(self, username):
        return self.control_command("snapshot", username=username)

    def submit(self, job_id, accounts=None, raw=None, version=1):
        result = self.runtime / "outbox" / (job_id + ".json")
        result.unlink(missing_ok=True)
        if raw is None:
            raw = json.dumps({"version": version, "job_id": job_id,
                              "operation": "sync_accounts", "accounts": accounts})
        self.atomic_write(self.runtime / "inbox" / (job_id + ".json"), raw)
        response = self.wait_json(result)
        assert response["status"] in ("ok", "partial", "error"), response
        assert isinstance(response.get("accounts"), list), "Result accounts must be a JSON array: %s" % response
        assert not any(secret in json.dumps(response) for secret in SECRETS)
        return response

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.control_command("shutdown")
                self.process.wait(timeout=10)
            except Exception:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.output:
            self.output.close()


def account(name, role="participant", admitted=True, password=None):
    value = {"username": name, "role": role, "admitted": admitted}
    if password is not None:
        value["initial_password"] = password
    return value


def run(h):
    h.start()
    assert h.snapshot("ben")["denial"] is False
    assert h.snapshot("unknown_player")["denial"]
    # Persisting an empty account map must not make the next startup fail closed.
    h.stop()
    h.start()
    assert h.snapshot("ben")["denial"] is False
    assert h.snapshot("unknown_player")["denial"]

    # An empty input sorts first; it must be consumed rather than starve this job.
    h.atomic_write(h.runtime / "inbox" / "00-empty.json", "")
    original_job = [account("bridge_demo", password=SECRETS[0])]
    initial = h.submit("create", original_job)
    assert initial["status"] == "ok" and initial["accounts"][0]["status"] == "created", initial
    empty_result = h.wait_json(h.runtime / "outbox" / "00-empty.json")
    assert empty_result["status"] == "error" and empty_result["accounts"] == [], empty_result
    assert not (h.runtime / "inbox" / "00-empty.json").exists()
    state = h.snapshot("bridge_demo")
    assert state["exists"] and state["denial"] is False
    assert state["privileges"].get("interact") and state["privileges"].get("shout")
    assert not state["privileges"].get("fly")
    assert h.submit("create", original_job) == initial
    conflict = h.submit("create", [account("bridge_demo", "facilitator")])
    assert conflict["status"] == "error" and conflict.get("code") == "job_id_conflict", conflict
    assert not h.snapshot("bridge_demo")["privileges"].get("fly")

    h.control_command("set_password", username="bridge_demo", password=SECRETS[1])
    changed_password = h.snapshot("bridge_demo")["password"]
    assert changed_password != state["password"]
    h.control_command("set_privilege", username="bridge_demo", privilege="creative")
    promoted = h.submit("promote", [account("bridge_demo", "facilitator", password=SECRETS[2])])
    assert promoted["accounts"][0]["status"] == "updated", promoted
    state = h.snapshot("bridge_demo")
    assert state["password"] == changed_password
    assert all(state["privileges"].get(p) for p in ("interact", "shout", "fly", "fast", "teleport", "creative"))
    unchanged = h.submit("unchanged", [account("bridge_demo", "facilitator")])
    assert unchanged["accounts"][0]["status"] == "unchanged", unchanged
    h.submit("demote", [account("bridge_demo")])
    state = h.snapshot("bridge_demo")
    assert not any(state["privileges"].get(p) for p in ("fly", "fast", "teleport"))
    assert state["privileges"].get("creative") and state["password"] == changed_password

    h.control_command("mock_online", username="bridge_demo")
    h.submit("withdraw", [account("bridge_demo", admitted=False)])
    state = h.snapshot("bridge_demo")
    assert state["denial"] and state["kicked"]
    assert state["password"] == changed_password and state["privileges"].get("interact")

    h.control_command("create_account", username="local_account", password=SECRETS[0])
    local_password = h.snapshot("local_account")["password"]
    collision = h.submit("collisions", [account("local_account", password=SECRETS[2]),
                                        account("other_demo", password=SECRETS[0])])
    assert collision["status"] == "partial", collision
    assert [r["status"] for r in collision["accounts"]] == ["rejected", "created"], collision
    assert h.snapshot("local_account")["password"] == local_password
    assert h.snapshot("local_account")["denial"]
    for job_id, name in (("case-collision", "BRIDGE_DEMO"), ("admin-collision", "ben")):
        rejection = h.submit(job_id, [account(name, password=SECRETS[0])])
        assert rejection["accounts"][0]["status"] == "rejected", rejection
    h.submit("mixed-create", [account("Mixed_Case", password=SECRETS[0])])
    h.control_command("create_account", username="mixed_case", password=SECRETS[1])
    conflicting_password = h.snapshot("mixed_case")["password"]
    managed_case = h.snapshot("Mixed_Case")
    rejection = h.submit("mixed-conflict", [account("Mixed_Case", "facilitator", admitted=False)])
    assert rejection["accounts"][0]["status"] == "rejected", rejection
    assert h.snapshot("Mixed_Case")["privileges"] == managed_case["privileges"]
    assert h.snapshot("Mixed_Case")["denial"] is False
    assert h.snapshot("mixed_case")["password"] == conflicting_password

    invalid = [
        ("malformed", "{"),
        ("oversized", " " * (256 * 1024 + 1)),
        ("null-accounts", json.dumps({"version": 1, "job_id": "null-accounts",
                                      "operation": "sync_accounts", "accounts": None})),
        ("null-admitted", [account("valid_before_null", password=SECRETS[0]),
                           account("invalid_null", admitted=None, password=SECRETS[0])]),
        ("unknown-role", [account("valid_before_invalid", password=SECRETS[0]),
                          account("invalid_role", "owner", password=SECRETS[0])]),
        ("duplicate-name", [account("duplicate", password=SECRETS[0]), account("duplicate", password=SECRETS[0])]),
        ("invalid-name", [account("invalid name", password=SECRETS[0])]),
        ("too-many", [account("many_%d" % n, password=SECRETS[0]) for n in range(101)]),
    ]
    for job_id, body in invalid:
        response = h.submit(job_id, raw=body) if isinstance(body, str) else h.submit(job_id, body)
        assert response["status"] == "error", response
        assert response["accounts"] == [], response
    assert not h.snapshot("duplicate")["exists"]
    assert not h.snapshot("invalid_role")["exists"]
    oversized_reuse = h.submit("oversized", [account("oversized_reuse", password=SECRETS[0])])
    assert oversized_reuse.get("code") == "job_id_conflict", oversized_reuse
    assert not h.snapshot("oversized_reuse")["exists"]
    assert not h.snapshot("valid_before_invalid")["exists"], "Malformed batch was partially applied"
    assert not h.snapshot("valid_before_null")["exists"], "Null-containing batch was partially applied"
    assert not h.snapshot("invalid_null")["exists"]
    no_password = h.submit("no-password", [account("missing_password")])
    assert no_password["accounts"][0]["status"] == "rejected", no_password

    h.stop()
    h.start()
    state = h.snapshot("bridge_demo")
    assert state["exists"] and state["denial"] and state["password"] == changed_password
    assert h.submit("create", original_job) == initial
    assert h.snapshot("bridge_demo")["denial"], "A duplicate old job reapplied admission"
    h.submit("readmit", [account("bridge_demo")])
    state = h.snapshot("bridge_demo")
    assert state["denial"] is False and state["password"] == changed_password
    assert h.snapshot("other_demo")["exists"], "Omitted account was removed"
    assert h.snapshot("ben")["denial"] is False
    run_access_windows(h)
    # Exercise the actual CLI and checked-in examples, including the final
    # readmission which deliberately restores the participant role.
    repo = Path(__file__).resolve().parent.parent
    for example in sorted((repo / "examples").glob("*.json")):
        submitted = json.loads(example.read_text())
        simulation = subprocess.run([
            "python3", str(repo / "tools" / "simulate.py"),
            "--runtime-dir", str(h.runtime), str(example),
        ], text=True, capture_output=True, timeout=20)
        assert simulation.returncode == 0, simulation.stdout + simulation.stderr
        for entry in submitted["accounts"]:
            snapshot = h.snapshot(entry["username"])
            assert snapshot["exists"]
            assert bool(snapshot["denial"]) == (not entry["admitted"])
            assert bool(snapshot["privileges"].get("fly")) == (entry["role"] == "facilitator")
            if "initial_password" in entry:
                assert entry["initial_password"] not in simulation.stdout
                SECRETS.append(entry["initial_password"])
    # Corrupt receipts cannot be mistaken for a successful replay. The bridge
    # retains input, halts imports, and denies non-admin logins until repaired.
    (h.runtime / "receipts" / "create.json").write_text('{"digest":"x","result":null}')
    (h.runtime / "outbox" / "create.json").unlink()
    h.atomic_write(h.runtime / "inbox" / "create.json", json.dumps({
        "version": 1, "job_id": "create", "operation": "sync_accounts",
        "accounts": original_job,
    }))
    deadline = time.monotonic() + 5
    while not h.snapshot("bridge_demo")["denial"] and time.monotonic() < deadline:
        time.sleep(0.1)
    assert h.snapshot("bridge_demo")["denial"]
    assert h.snapshot("ben")["denial"] is False
    assert not (h.runtime / "outbox" / "create.json").exists()
    assert (h.runtime / "inbox" / "create.json").exists()
    (h.runtime / "inbox" / "create.json").unlink()
    h.stop()
    for path in [h.path / "engine.log", h.path / "process.log", *list(h.runtime.rglob("*.json"))]:
        if path.exists():
            assert not any(secret in path.read_text(errors="replace") for secret in SECRETS), "Password leaked in %s" % path.name
    assert not list((h.runtime / "inbox").glob("*.json")), "Consumed inputs were retained"


def run_access_windows(h):
    start, end = 1800000000, 1800000060
    window = {"starts_at": start, "ends_at": end}
    entry = {**account("timed_demo", password=SECRETS[0]), "access_window": window}
    h.control_command("set_time", timestamp=start - 1)
    initial = h.submit("timed-create", [entry], version=2)
    assert initial["version"] == 2 and initial["accounts"][0]["status"] == "created", initial
    assert h.snapshot("timed_demo")["denial"]
    h.control_command("set_time", timestamp=start)
    assert h.snapshot("timed_demo")["denial"] is False
    h.control_command("set_password", username="timed_demo", password=SECRETS[1])
    password = h.snapshot("timed_demo")["password"]
    updated = h.submit("timed-v1-update", [account("timed_demo", "facilitator", password=SECRETS[2])])
    assert updated["version"] == 1
    checkpoint = json.loads((h.runtime / "state.json").read_text())
    assert checkpoint["accounts"]["timed_demo"]["access_window"] == window
    assert h.snapshot("timed_demo")["password"] == password
    h.control_command("set_time", timestamp=end - 1)
    assert h.snapshot("timed_demo")["denial"] is False
    h.control_command("mock_online", username="timed_demo")
    h.control_command("set_time", timestamp=end)
    deadline = time.monotonic() + 2
    while not h.snapshot("timed_demo")["kicked"] and time.monotonic() < deadline:
        time.sleep(.05)
    assert h.snapshot("timed_demo")["denial"] and h.snapshot("timed_demo")["kicked"]
    assert h.snapshot("ben")["denial"] is False
    # An old successful receipt cannot override a subsequent timed withdrawal.
    h.submit("timed-withdraw", [{**entry, "admitted": False}], version=2)
    assert h.submit("timed-create", [entry], version=2) == initial
    h.control_command("set_time", timestamp=start)
    assert h.snapshot("timed_demo")["denial"]
    h.submit("timed-readmit", [{**entry, "role": "facilitator"}], version=2)
    assert h.snapshot("timed_demo")["denial"] is False
    h.stop()
    h.start()
    h.control_command("set_time", timestamp=end)
    assert h.snapshot("timed_demo")["denial"]
    assert h.snapshot("timed_demo")["password"] == password
    assert h.submit("timed-create", [entry], version=2) == initial
    assert h.snapshot("timed_demo")["denial"]
    # Late creation remains outside its access period, even with admitted=true.
    late = {**account("late_demo", password=SECRETS[0]), "access_window": window}
    assert h.submit("timed-late", [late], version=2)["accounts"][0]["status"] == "created"
    assert h.snapshot("late_demo")["denial"]
    invalid = [None, {}, {"starts_at": start}, {"starts_at": True, "ends_at": end},
               {"starts_at": start + .5, "ends_at": end}, {"starts_at": start, "ends_at": start},
               {"starts_at": end, "ends_at": start}, {"starts_at": start, "ends_at": 2**53},
               {**window, "unexpected": 1}]
    for index, value in enumerate(invalid):
        invalid_entry = {**account("invalid_timed", password=SECRETS[0]), "access_window": value}
        response = h.submit("invalid-window-%d" % index, [invalid_entry], version=2)
        assert response["version"] == 2 and response.get("code") == "invalid_access_window", response
        assert not h.snapshot("invalid_timed")["exists"]
    missing = h.submit("missing-window", [account("missing_timed", password=SECRETS[0])], version=2)
    assert missing.get("code") == "invalid_access_window"
    assert h.submit("v1-window", [entry]).get("code") == "invalid_access_window"
    # Invalid persisted windows fail closed on restart.
    h.stop()
    checkpoint = json.loads((h.runtime / "state.json").read_text())
    original_checkpoint = json.dumps(checkpoint)
    checkpoint["accounts"]["timed_demo"]["access_window"]["ends_at"] = start
    (h.runtime / "state.json").write_text(json.dumps(checkpoint))
    h.start()
    assert h.snapshot("bridge_demo")["denial"]
    assert h.snapshot("ben")["denial"] is False
    h.stop()
    (h.runtime / "state.json").write_text(original_checkpoint)
    h.start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=shutil.which("luanti") or shutil.which("minetest") or shutil.which("minetestserver"))
    parser.add_argument("--base", type=Path, default=Path.home(), help="Parent for the disposable test directory")
    parser.add_argument("--game-path", type=Path, default=Path("/home/ben/.minetest/games/minetest_game"), help="Installed Minetest Game directory (or its parent)")
    parser.add_argument("--keep", action="store_true", help="Retain test world even on success")
    args = parser.parse_args()
    if not args.engine:
        parser.error("Luanti was not found; pass --engine /path/to/luanti")
    mod = Path(__file__).resolve().parent.parent
    h = Harness(args.engine, mod, args.base, args.game_path)
    success = False
    try:
        run(h)
        success = True
        print("PASS: real-engine bridge integration, replay, admission, passwords, validation and restart")
    finally:
        h.stop()
        if success and not args.keep:
            shutil.rmtree(h.path)
        else:
            print("Test artifacts: %s" % h.path)


if __name__ == "__main__":
    main()
