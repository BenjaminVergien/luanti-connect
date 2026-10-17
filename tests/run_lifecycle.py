#!/usr/bin/env python3
"""Test lifecycle operations with owned Luanti processes and disposable worlds.

Never talks to systemd or uses the live development world. Standard-library only.
"""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lifecycle.controller import LifecycleController, LifecycleError, SystemdBackend


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.replace(path)


class EngineBackend:
    """A service-shaped adapter restricted to processes this test starts."""

    def __init__(self, config, state_path, artifacts):
        self.config = config
        self.state_path = state_path
        self.artifacts = artifacts
        self.process = None
        self.world = None
        self.output = None
        self.ready_file = None
        self.starts = 0
        self.stops = 0
        self.sequence = 0

    def start(self, world_id=None, world_path=None):
        assert not self.process or self.process.poll() is not None, "Starting a second engine"
        selected = json.loads(self.state_path.read_text())["world_id"]
        self.world = Path(self.config["worlds"][selected])
        self.starts += 1
        self.output = (self.artifacts / ("process-%d.log" % self.starts)).open("wb")
        self.sequence += 1
        self.ready_file = self.world / "test-control" / ("response-%d.json" % self.sequence)
        self.ready_file.unlink(missing_ok=True)
        atomic_json(self.world / "test-control" / ("command-%d.json" % self.sequence), {"operation": "ready"})
        environment = dict(os.environ, HOME=self.config["user_home"])
        self.process = subprocess.Popen(
            [self.config["engine"], "--server", "--world", str(self.world),
             "--gameid", self.config["game_id"], "--config", self.config["server_config"],
             "--logfile", self.config["logfile"]],
            env=environment, stdout=self.output, stderr=subprocess.STDOUT,
        )

    def info(self):
        active = self.process is not None and self.process.poll() is None
        return {"active": active, "pid": self.process.pid if active else 0,
                "world_path": str(self.world) if active else None,
                "ready": bool(active and self.ready_file and self.ready_file.is_file())}

    def stop(self):
        self.stops += 1
        if self.process and self.process.poll() is None:
            try:
                self.command("shutdown")
                self.process.wait(timeout=8)
            except (AssertionError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.output:
            self.output.close()

    def wait_json(self, path, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.process and self.process.poll() is None, "Engine exited; inspect lifecycle test artifacts"
            try:
                return json.loads(path.read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                time.sleep(0.05)
        raise AssertionError("Timed out waiting for %s" % path)

    def command(self, operation, **values):
        self.sequence += 1
        directory = self.world / "test-control"
        response = directory / ("response-%d.json" % self.sequence)
        atomic_json(directory / ("command-%d.json" % self.sequence), dict(operation=operation, **values))
        result = self.wait_json(response)
        assert "error" not in result, result
        response.unlink()
        return result

    def import_account(self, job_id, username, admitted):
        runtime = self.world / "civicrm_bridge"
        atomic_json(runtime / "inbox" / (job_id + ".json"), {
            "version": 1, "job_id": job_id, "operation": "sync_accounts",
            "accounts": [{"username": username, "initial_password": "Lifecycle-Fictional-Password!",
                          "role": "participant", "admitted": admitted}],
        })
        result = self.wait_json(runtime / "outbox" / (job_id + ".json"))
        assert result["status"] == "ok", result
        return self.command("snapshot", username=username)


def expect_failure(operation, description):
    try:
        operation()
    except LifecycleError as error:
        assert error.code, "LifecycleError must include a machine-readable code"
        return error
    raise AssertionError("Expected lifecycle failure: " + description)


def create_fixture(base, engine, game):
    artifacts = Path(tempfile.mkdtemp(prefix="civicrm-lifecycle-test-", dir=base))
    home = artifacts / "home"
    games = home / ".minetest" / "games"
    games.mkdir(parents=True)
    (games / "minetest_game").symlink_to(game.resolve(), target_is_directory=True)
    mods = home / ".minetest" / "mods"
    mods.mkdir()
    (mods / "civicrm_bridge").symlink_to(ROOT, target_is_directory=True)
    driver = mods / "bridge_test_driver"
    driver.mkdir()
    (driver / "mod.conf").write_text("name = bridge_test_driver\ndepends = civicrm_bridge\n")
    shutil.copyfile(ROOT / "tests" / "driver.lua", driver / "init.lua")
    worlds = {}
    for name in ("dev", "test"):
        world = artifacts / name
        (world / "worldmods").mkdir(parents=True)
        world.chmod(0o700)
        (world / "worldmods").chmod(0o700)
        # The SSH account uses a group-writable umask. Keep both initial and
        # engine-rewritten executable settings private to the fixture owner.
        subprocess.run(["/usr/bin/setfacl", "-d", "-m", "u::rwx,g::---,o::---", str(world)], check=True)
        (world / "test-control").mkdir()
        (world / "world.mt").write_text(
            "gameid = minetest\nbackend = sqlite3\nplayer_backend = sqlite3\n"
            "auth_backend = sqlite3\nmod_storage_backend = sqlite3\n"
            "load_mod_civicrm_bridge = mods/civicrm_bridge\n"
            "load_mod_bridge_test_driver = mods/bridge_test_driver\n"
        )
        worlds[name] = str(world)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server_config = artifacts / "server.conf"
    server_config.write_text("name = ben\nbind_address = 127.0.0.1\nport = %d\n"
                             "server_announce = false\nenable_ipv6 = false\n" % port)
    config = {"service": "isolated-lifecycle-test.service", "engine": str(engine),
              "game_id": "minetest", "user_home": str(home),
              "server_config": str(server_config), "logfile": str(artifacts / "engine.log"),
              "worlds": worlds}
    state = artifacts / "state.json"
    atomic_json(state, {"world_id": "dev"})
    return artifacts, config, state, artifacts / "controller.lock"


def check_selected(state, expected):
    assert json.loads(state.read_text())["world_id"] == expected


def run_real(config, state, lock, backend):
    controller = LifecycleController(config, backend, state, lock, timeout=12, poll_interval=0.05)
    assert isinstance(controller.status(), dict)
    assert not backend.info()["active"]
    controller.start_world("dev")
    assert backend.info()["ready"] and backend.info()["world_path"] == config["worlds"]["dev"]
    check_selected(state, "dev")
    dev_pid = backend.info()["pid"]
    starts, stops = backend.starts, backend.stops
    controller.start_world("dev")
    controller.switch_world("dev")
    assert backend.info()["pid"] == dev_pid
    assert (backend.starts, backend.stops) == (starts, stops), "Same-world request restarted service"
    expect_failure(lambda: controller.switch_world("unknown"), "unknown world")
    assert backend.info()["pid"] == dev_pid and backend.stops == stops
    check_selected(state, "dev")
    expect_failure(lambda: controller.start_world("test"), "start cannot implicitly switch worlds")
    assert backend.info()["pid"] == dev_pid and backend.stops == stops

    # A separate owner of the same lock prevents all lifecycle mutation.
    with lock.open("a+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        expect_failure(lambda: controller.switch_world("test"), "concurrent operation")
        assert backend.info()["pid"] == dev_pid and backend.stops == stops
        check_selected(state, "dev")
        fcntl.flock(held, fcntl.LOCK_UN)

    dev_account = backend.import_account("dev-create", "dev_only", True)
    assert dev_account["exists"] and dev_account["denial"] is False
    backend.command("set_password", username="dev_only", password="Lifecycle-Changed-Password!")
    password = backend.command("snapshot", username="dev_only")["password"]
    backend.import_account("dev-deny", "dev_only", False)
    controller.switch_world("test")
    check_selected(state, "test")
    assert backend.info()["ready"] and backend.info()["world_path"] == config["worlds"]["test"]
    assert not backend.command("snapshot", username="dev_only")["exists"]
    test_account = backend.import_account("test-create", "test_only", True)
    assert test_account["exists"] and test_account["denial"] is False
    assert not (Path(config["worlds"]["test"]) / "civicrm_bridge" / "outbox" / "dev-create.json").exists()

    controller.switch_world("dev")
    check_selected(state, "dev")
    restored = backend.command("snapshot", username="dev_only")
    assert restored["exists"] and restored["denial"] and restored["password"] == password
    assert not backend.command("snapshot", username="test_only")["exists"]
    assert not (Path(config["worlds"]["dev"]) / "civicrm_bridge" / "outbox" / "test-create.json").exists()

    controller.stop_world()
    assert not backend.info()["active"]
    check_selected(state, "dev")
    controller.stop_world()
    controller.start_world("dev")
    assert backend.info()["ready"]
    assert backend.command("snapshot", username="dev_only")["password"] == password

    # Invalid configured world is rejected before the running world is stopped.
    test_mt = Path(config["worlds"]["test"]) / "world.mt"
    original = test_mt.read_text()
    test_mt.write_text(original.replace("gameid = minetest", "gameid = nonexistent_game"))
    before = backend.info()["pid"]
    before_stops = backend.stops
    try:
        expect_failure(lambda: controller.switch_world("test"), "wrong configured game")
        assert backend.info()["pid"] == before and backend.stops == before_stops
        check_selected(state, "dev")
    finally:
        test_mt.write_text(original)

    duplicate_mod = Path(config["worlds"]["test"]) / "worldmods" / "civicrm_bridge"
    duplicate_mod.symlink_to(ROOT, target_is_directory=True)
    try:
        expect_failure(lambda: controller.switch_world("test"), "duplicate world-local bridge copy")
        assert backend.info()["pid"] == before and backend.stops == before_stops
        check_selected(state, "dev")
    finally:
        duplicate_mod.unlink()

    # A trusted shared test mod fails loading, exercising startup rollback.
    broken = Path(config["user_home"]) / ".minetest" / "mods" / "broken_dependency"
    broken.mkdir()
    (broken / "mod.conf").write_text("name = broken_dependency\ndepends = lifecycle_missing_dependency\n")
    (broken / "init.lua").write_text("-- Dependency failure intentionally prevents startup.\n")
    test_mt.write_text(original + "load_mod_broken_dependency = mods/broken_dependency\n")
    before_starts = backend.starts
    try:
        expect_failure(lambda: controller.switch_world("test"), "target failed to start")
        assert backend.starts >= before_starts + 2, "Failed target was not attempted and rolled back"
        assert backend.info()["active"] and backend.info()["ready"]
        assert backend.info()["world_path"] == config["worlds"]["dev"]
        check_selected(state, "dev")
        restored = backend.command("snapshot", username="dev_only")
        assert restored["denial"] and restored["password"] == password
    finally:
        test_mt.write_text(original)
        shutil.rmtree(broken)
    controller.switch_world("test")
    assert backend.command("snapshot", username="test_only")["denial"] is False
    controller.switch_world("dev")
    controller.stop_world()


class NeverReadyBackend:
    def __init__(self, config, state):
        self.config, self.state = config, state
        self.active, self.world = False, None
        self.starts = self.stops = 0

    def info(self):
        return {"active": self.active, "pid": 123456 if self.active else 0,
                "world_path": self.world if self.active else None, "ready": False}

    def start(self, world_id=None, world_path=None):
        self.starts += 1
        self.active = True
        self.world = self.config["worlds"][json.loads(self.state.read_text())["world_id"]]

    def stop(self):
        self.stops += 1
        self.active = False


def run_timeout(config, state, lock):
    atomic_json(state, {"world_id": "dev"})
    backend = NeverReadyBackend(config, state)
    controller = LifecycleController(config, backend, state, lock, timeout=0.2, poll_interval=0.02)
    began = time.monotonic()
    expect_failure(lambda: controller.start_world("test"), "readiness timeout")
    assert time.monotonic() - began < 3, "Readiness timeout was not bounded"
    assert backend.starts == 1 and backend.stops >= 1
    assert not backend.active, "Failed startup was left running"
    check_selected(state, "dev")

    class RefusesStopBackend(NeverReadyBackend):
        def stop(self):
            self.stops += 1

    # Failure to stop an existing process must prevent selection and startup.
    stubborn = RefusesStopBackend(config, state)
    stubborn.active = True
    stubborn.world = config["worlds"]["dev"]
    controller = LifecycleController(config, stubborn, state, lock, timeout=0.2, poll_interval=0.02)
    expect_failure(lambda: controller.switch_world("test"), "existing process refuses to stop")
    assert stubborn.starts == 0 and stubborn.active
    check_selected(state, "dev")

    # Failure to stop a failed target must not launch the previous world beside it.
    stubborn = RefusesStopBackend(config, state)
    controller = LifecycleController(config, stubborn, state, lock, timeout=0.2, poll_interval=0.02)
    error = expect_failure(lambda: controller.start_world("test"), "failed target refuses cleanup")
    assert stubborn.starts == 1 and stubborn.active
    assert error.details.get("rollback") == "failed", "Failed rollback was not reported"
    check_selected(state, "test")


def run_systemd_fixtures(config, state, lock):
    """Exercise systemctl/journalctl semantics without calling either program."""
    pid = 4242
    invocation = "a" * 32
    original_read_bytes = Path.read_bytes
    command = [config["engine"], "--server", "--world", config["worlds"]["dev"]]

    def inspect(active="active", main_pid=pid, arguments=None, journal_code=0, logs=""):
        arguments = command if arguments is None else arguments
        properties = "ActiveState=%s\nSubState=running\nMainPID=%d\nInvocationID=%s\n" % (active, main_pid, invocation)

        def fake_run(argv, **options):
            if argv[:2] == ["/usr/bin/systemctl", "show"]:
                code, output = 0, properties
            elif argv[0] == "/usr/bin/journalctl":
                assert "_SYSTEMD_INVOCATION_ID=" + invocation in argv
                assert any(argument.startswith("--grep=") for argument in argv)
                code, output = journal_code, logs
            else:
                raise AssertionError("Unexpected systemd fixture command: %r" % argv)
            if options.get("check") and code:
                raise subprocess.CalledProcessError(code, argv, output=output, stderr="fixture error" if code > 1 else "")
            return subprocess.CompletedProcess(argv, code, stdout=output, stderr="fixture error" if code > 1 else "")

        def fake_cmdline(path):
            if str(path) == "/proc/%d/cmdline" % main_pid:
                return b"\0".join(argument.encode() for argument in arguments) + b"\0"
            return original_read_bytes(path)

        with patch("lifecycle.controller.subprocess.run", side_effect=fake_run), patch.object(Path, "read_bytes", fake_cmdline):
            return SystemdBackend(config).info()

    waiting = inspect(journal_code=1)
    assert waiting["active"] and not waiting["ready"]
    assert waiting["world_path"] == config["worlds"]["dev"]
    error = expect_failure(lambda: inspect(journal_code=2), "journalctl read failure")
    assert error.code == "service_error"
    ready = inspect(logs='[civicrm_bridge] Ready; local JSON inbox enabled\nServer for gameid="minetest" listening on 127.0.0.1:30000\n')
    assert ready["ready"]

    for phase in ("activating", "deactivating"):
        transient = inspect(active=phase, main_pid=0)
        assert transient["active"] and transient["transitioning"] and not transient["ready"]
        assert transient["world_path"] is None
    launcher = inspect(arguments=["/usr/bin/python3", "-I", "/usr/local/lib/civicrm-worldctl/launch.py"])
    assert launcher["transitioning"] and not launcher["ready"]
    assert launcher["world_path"] is None
    foreign = inspect(active="activating", arguments=["/usr/bin/sleep", "1000"])
    assert foreign["active"] and not foreign.get("transitioning") and foreign["world_path"] is None

    class TransitionBackend(NeverReadyBackend):
        def __init__(self, config, state):
            super().__init__(config, state)
            self.active = True
            self.transitioning = True

        def info(self):
            return {"active": self.active, "pid": 0 if self.transitioning or not self.active else pid,
                    "world_path": self.world if self.active else None,
                    "ready": self.active and not self.transitioning,
                    "transitioning": self.active and self.transitioning}

        def start(self, world_id=None, world_path=None):
            super().start()
            self.transitioning = False

        def stop(self):
            super().stop()
            self.transitioning = False

    atomic_json(state, {"world_id": "dev"})
    transient = TransitionBackend(config, state)
    controller = LifecycleController(config, transient, state, lock, timeout=0.2, poll_interval=0.02)
    status = controller.status()
    assert status["world_id"] == "dev" and status["state"] == "starting" and not status["ready"]
    controller.stop_world()
    assert transient.stops == 1 and not transient.active
    transient = TransitionBackend(config, state)
    controller = LifecycleController(config, transient, state, lock, timeout=0.2, poll_interval=0.02)
    controller.switch_world("test")
    assert transient.stops == 1 and transient.starts == 1
    assert controller.status()["world_id"] == "test" and controller.status()["ready"]

    # A foreign executable must still be protected from stop/switch operations.
    unmanaged = NeverReadyBackend(config, state)
    unmanaged.active = True
    controller = LifecycleController(config, unmanaged, state, lock, timeout=0.2, poll_interval=0.02)
    assert controller.status()["state"] == "unmanaged"
    expect_failure(controller.stop_world, "foreign active process")
    expect_failure(lambda: controller.switch_world("dev"), "foreign active process switch")
    assert unmanaged.stops == 0 and unmanaged.starts == 0


def run_fifo_fixture(config, state, lock):
    atomic_json(state, {"world_id": "dev"})
    backend = NeverReadyBackend(config, state)
    backend.active = True
    backend.world = config["worlds"]["dev"]
    controller = LifecycleController(config, backend, state, lock, timeout=0.2, poll_interval=0.02)
    settings = Path(config["worlds"]["test"]) / "world.mt"
    original = settings.read_text()

    def timed_out(_signal, _frame):
        raise AssertionError("FIFO world.mt blocked lifecycle validation")

    old_handler = signal.signal(signal.SIGALRM, timed_out)
    settings.unlink()
    os.mkfifo(settings)
    try:
        signal.alarm(2)
        expect_failure(lambda: controller.switch_world("test"), "FIFO world.mt")
        signal.alarm(0)
        assert backend.stops == 0 and backend.starts == 0 and backend.active
        check_selected(state, "dev")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        settings.unlink()
        settings.write_text(original)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", default=shutil.which("luanti") or "/usr/bin/luanti")
    parser.add_argument("--base", type=Path, default=Path.home())
    parser.add_argument("--game-path", type=Path, default=Path("/home/ben/.minetest/games/minetest_game"))
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--mock-only", action="store_true", help="Run controller/systemd regression fixtures without starting Luanti")
    args = parser.parse_args()
    artifacts, config, state, lock = create_fixture(args.base, args.engine, args.game_path)
    backend = EngineBackend(config, state, artifacts)
    success = False
    try:
        if not args.mock_only:
            run_real(config, state, lock, backend)
        run_timeout(config, state, lock)
        run_systemd_fixtures(config, state, lock)
        run_fifo_fixture(config, state, lock)
        success = True
        print("PASS: lifecycle timeout, systemd transition/readiness and FIFO safety fixtures" if args.mock_only else
              "PASS: real-engine lifecycle transitions, rollback, lock, timeout, world isolation and systemd safety fixtures")
    finally:
        backend.stop()
        if success and not args.keep:
            shutil.rmtree(artifacts)
        else:
            print("Lifecycle artifacts: %s" % artifacts)


if __name__ == "__main__":
    main()
