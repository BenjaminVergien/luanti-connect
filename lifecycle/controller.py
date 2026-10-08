"""Manage one Luanti service using administrator-configured world IDs."""
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import tempfile
import time


class LifecycleError(Exception):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def check_trusted_permissions(descriptor, trusted_uids, allow_sticky=False):
    """Check effective Linux ACL writes, not just the ACL mask in st_mode."""
    metadata = os.fstat(descriptor)
    if metadata.st_uid not in trusted_uids:
        raise LifecycleError("invalid_world", "World paths must be owned by root or the service user.")
    # A root-owned sticky temporary directory cannot replace another user's
    # existing child. Only ancestors, never the world itself, get this exception.
    if allow_sticky and metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX:
        return
    for attribute in ("system.posix_acl_access", "system.posix_acl_default"):
        try:
            raw = os.getxattr(descriptor, attribute)
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            if attribute.endswith("access") and metadata.st_mode & 0o022:
                raise LifecycleError("invalid_world", "World paths cannot be writable by other users.")
            continue
        if len(raw) < 4 or struct.unpack_from("<I", raw)[0] != 2 or (len(raw) - 4) % 8:
            raise LifecycleError("invalid_world", "World path has an invalid filesystem ACL.")
        entries = list(struct.iter_unpack("<HHI", raw[4:]))
        mask = next((permissions for tag, permissions, _ in entries if tag == 16), 7)
        for tag, permissions, identity in entries:
            effective = permissions & mask if tag in (2, 4, 8) else permissions
            if effective & 2 and (tag in (4, 8, 32) or (tag == 2 and identity not in trusted_uids)):
                raise LifecycleError("invalid_world", "World path ACL grants write access to another user.")


@contextmanager
def trusted_world_directory(path, trusted_uids):
    """Hold a no-follow world descriptor after checking every ancestor."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open("/", flags)
    try:
        check_trusted_permissions(descriptor, trusted_uids, allow_sticky=True)
        for index, component in enumerate(path.parts[1:]):
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            check_trusted_permissions(descriptor, trusted_uids,
                                      allow_sticky=index < len(path.parts) - 2)
        yield descriptor
    finally:
        os.close(descriptor)


def load_json(path):
    try:
        with Path(path).open() as stream:
            value = json.load(stream)
    except (OSError, ValueError) as error:
        raise LifecycleError("invalid_configuration", "Cannot read lifecycle configuration or state.") from error
    if not isinstance(value, dict):
        raise LifecycleError("invalid_configuration", "Lifecycle configuration must be a JSON object.")
    return value


def trusted_json(path):
    path = Path(path)
    if path.is_symlink() or path.parent.is_symlink():
        raise LifecycleError("unsafe_configuration", "Lifecycle configuration cannot be a symlink.")
    for item in (path, path.parent):
        metadata = item.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise LifecycleError("unsafe_configuration", "Lifecycle files must be root-owned and not writable by other users.")
    return load_json(path)


def launch_arguments(config, world_id):
    if world_id not in config["worlds"]:
        raise LifecycleError("unknown_world", "World ID is not configured.")
    return [config["engine"], "--server", "--gameid", config["game_id"],
            "--world", config["worlds"][world_id], "--config", config["server_config"],
            "--logfile", config["logfile"]]


class SystemdBackend:
    def __init__(self, config):
        self.config = config
        self.service = config["service"]
        if not re.fullmatch(r"[A-Za-z0-9_.@-]+\.service", self.service):
            raise LifecycleError("invalid_configuration", "Invalid service name.")
        self.ready_instance = None
        self.environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C"}

    def _run(self, arguments, timeout=10):
        try:
            return subprocess.run(arguments, text=True, capture_output=True,
                                  timeout=timeout, check=True, env=self.environment,
                                  stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError) as error:
            raise LifecycleError("service_error", "A service management command failed.") from error

    def _readiness_logs(self, invocation):
        arguments = ["/usr/bin/journalctl", "--no-pager", "--quiet", "--output=cat",
                     "--lines=20", "--grep=\\[civicrm_bridge\\] Ready;|Server for gameid=",
                     "_SYSTEMD_INVOCATION_ID=" + invocation]
        try:
            result = subprocess.run(arguments, text=True, capture_output=True,
                                    timeout=10, check=False, env=self.environment,
                                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as error:
            raise LifecycleError("service_error", "Cannot query service readiness.") from error
        # journalctl --grep exits 1 when the current invocation has no matches yet.
        if result.returncode not in (0, 1):
            raise LifecycleError("service_error", "Cannot query service readiness.")
        return result.stdout if result.returncode == 0 else ""

    def info(self):
        properties = self._run(["/usr/bin/systemctl", "show", self.service,
                                "--property=ActiveState,SubState,MainPID,InvocationID"])
        values = dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)
        pid = int(values.get("MainPID", "0"))
        active = values.get("ActiveState") in ("active", "activating", "deactivating") or pid > 0
        world_path = None
        transitioning = active and pid == 0
        if pid:
            try:
                arguments = Path("/proc/%d/cmdline" % pid).read_bytes().split(b"\0")
                arguments = [item.decode() for item in arguments if item]
                if arguments and Path(arguments[0]).resolve() == Path(self.config["engine"]).resolve():
                    index = arguments.index("--world")
                    world_path = arguments[index + 1]
                elif arguments == ["/usr/bin/python3", "-I", "/usr/local/lib/civicrm-worldctl/launch.py"]:
                    transitioning = True
            except (OSError, ValueError, IndexError, UnicodeError):
                pass
        invocation = values.get("InvocationID", "")
        instance = (pid, invocation)
        ready = False
        if values.get("ActiveState") == "active" and world_path and re.fullmatch(r"[0-9a-f]{32}", invocation):
            if self.ready_instance == instance:
                ready = True
            else:
                logs = self._readiness_logs(invocation)
                ready = ("[civicrm_bridge] Ready;" in logs and
                         'Server for gameid="%s" listening on ' % self.config["game_id"] in logs)
                if ready:
                    self.ready_instance = instance
        return {"active": active, "pid": pid, "world_path": world_path,
                "ready": ready, "transitioning": bool(transitioning)}

    def start(self):
        self.ready_instance = None
        self._run(["/usr/bin/systemctl", "start", self.service], timeout=40)

    def stop(self):
        self.ready_instance = None
        self._run(["/usr/bin/systemctl", "stop", self.service], timeout=40)


class LifecycleController:
    def __init__(self, config, backend, state_path, lock_path, timeout=30, poll_interval=0.2):
        self.config = config
        self.backend = backend
        self.state_path = Path(state_path)
        self.lock_path = Path(lock_path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        if not isinstance(config.get("worlds"), dict) or not config["worlds"]:
            raise LifecycleError("invalid_configuration", "No worlds are configured.")
        resolved = set()
        for name, path in config["worlds"].items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", name):
                raise LifecycleError("invalid_configuration", "Invalid configured world ID.")
            if not isinstance(path, str) or not Path(path).is_absolute():
                raise LifecycleError("invalid_configuration", "World paths must be absolute.")
            canonical = str(Path(path).resolve())
            if canonical in resolved:
                raise LifecycleError("invalid_configuration", "Two IDs refer to the same world.")
            resolved.add(canonical)

    @contextmanager
    def _lock(self):
        try:
            descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        except OSError as error:
            raise LifecycleError("lock_error", "Cannot open the lifecycle operation lock.") from error
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LifecycleError("busy", "Another lifecycle operation is running.") from error
            yield
        finally:
            os.close(descriptor)

    def _selected(self):
        if self.state_path.is_symlink():
            raise LifecycleError("unsafe_state", "Lifecycle state cannot be a symlink.")
        world_id = load_json(self.state_path).get("world_id")
        if world_id not in self.config["worlds"]:
            raise LifecycleError("invalid_state", "Selected world is not configured.")
        return world_id

    def _select(self, world_id):
        descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=self.state_path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                os.fchmod(stream.fileno(), 0o644)
                json.dump({"version": 1, "world_id": world_id}, stream)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def validate_world(self, world_id):
        if not isinstance(world_id, str) or world_id not in self.config["worlds"]:
            raise LifecycleError("unknown_world", "World ID is not configured.")
        path = Path(self.config["worlds"][world_id])
        if not path.is_dir() or path.resolve() != path or (path / "world.mt").is_symlink():
            raise LifecycleError("invalid_world", "Configured world directory is missing or uses a symlink.")
        try:
            settings = {}
            trusted_uids = {0, Path(self.config["user_home"]).stat().st_uid}
            with trusted_world_directory(path, trusted_uids) as world:
                try:
                    mods = os.open("worldmods", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                   dir_fd=world)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        check_trusted_permissions(mods, trusted_uids)
                        if os.listdir(mods):
                            raise LifecycleError("invalid_world", "World-local mods are not permitted.")
                    finally:
                        os.close(mods)
                descriptor = os.open("world.mt", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=world)
                with os.fdopen(descriptor) as stream:
                    metadata = os.fstat(stream.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                        raise LifecycleError("invalid_world", "World settings must be a regular file of at most 64 KiB.")
                    check_trusted_permissions(stream.fileno(), trusted_uids)
                    contents = stream.read(65537)
            if len(contents) > 65536:
                raise LifecycleError("invalid_world", "World settings exceed the size limit.")
            for line in contents.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = (item.strip() for item in stripped.split("=", 1))
                if key in settings:
                    raise LifecycleError("invalid_world", "World configuration contains duplicate settings.")
                settings[key] = value
        except OSError as error:
            raise LifecycleError("invalid_world", "Cannot read configured world settings.") from error
        if settings.get("gameid") != self.config["game_id"]:
            raise LifecycleError("invalid_world", "World uses a different game.")
        if settings.get("load_mod_civicrm_bridge") != "mods/civicrm_bridge":
            raise LifecycleError("invalid_world", "World must explicitly enable the shared bridge.")
        shared = Path(self.config["user_home"]) / ".minetest" / "mods" / "civicrm_bridge"
        if not (shared / "mod.conf").is_file() or not Path(self.config["server_config"]).is_file():
            raise LifecycleError("invalid_world", "Shared bridge or server configuration is missing.")
        if not Path(self.config["engine"]).is_file() or not os.access(self.config["engine"], os.X_OK):
            raise LifecycleError("invalid_configuration", "Configured Luanti executable is unavailable.")
        return path

    def _status(self):
        selected = self._selected()
        info = self.backend.info()
        world_id = next((name for name, path in self.config["worlds"].items()
                         if path == info.get("world_path")), None)
        if world_id is None and info.get("transitioning"):
            world_id = selected
        ready = bool(info["active"] and world_id and info.get("ready"))
        state = "stopped" if not info["active"] else ("unmanaged" if not world_id else ("running" if ready else "starting"))
        return {"ok": True, "state": state, "world_id": world_id if info["active"] else None,
                "selected_world_id": selected, "active": bool(info["active"]),
                "ready": ready, "pid": info.get("pid", 0)}

    def status(self):
        with self._lock():
            return self._status()

    def _wait_stopped(self):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            info = self.backend.info()
            if not info["active"] and not info.get("pid"):
                return
            time.sleep(self.poll_interval)
        raise LifecycleError("stop_timeout", "Luanti did not stop within the timeout.")

    def _wait_ready(self, world_id):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            status = self._status()
            if status["world_id"] == world_id and status["ready"]:
                return status
            time.sleep(self.poll_interval)
        raise LifecycleError("startup_timeout", "Target world did not become ready within the timeout.")

    def stop_world(self):
        with self._lock():
            initial = self._status()
            if initial["active"] and initial["world_id"] is None:
                raise LifecycleError("unmanaged_instance", "Service is running an unconfigured world or a different executable.")
            if initial["active"]:
                self.backend.stop()
                self._wait_stopped()
            return dict(self._status(), operation="stop_world", changed=initial["active"])

    def _change(self, world_id, allow_switch):
        # All target checks happen before the current process is disturbed.
        self.validate_world(world_id)
        initial = self._status()
        if initial["active"] and initial["world_id"] is None:
            raise LifecycleError("unmanaged_instance", "Service is running an unconfigured world or a different executable.")
        operation = "switch_world" if allow_switch else "start_world"
        if initial["active"] and initial["world_id"] == world_id:
            if not initial["ready"]:
                initial = self._wait_ready(world_id)
            self._select(world_id)
            return dict(initial, selected_world_id=world_id, operation=operation, changed=False)
        if initial["active"] and not allow_switch:
            raise LifecycleError("already_running", "A different world is running; use switch_world.")
        previous = initial["world_id"] if initial["active"] else initial["selected_world_id"]
        if initial["active"]:
            self.backend.stop()
            self._wait_stopped()
        try:
            self._select(world_id)
            self.backend.start()
            result = self._wait_ready(world_id)
        except (LifecycleError, OSError) as error:
            cause = getattr(error, "code", "state_error")
            try:
                # Never launch the old world until the failed target has stopped.
                self.backend.stop()
                self._wait_stopped()
                self._select(previous)
                if initial["active"]:
                    self.validate_world(previous)
                    self.backend.start()
                    self._wait_ready(previous)
                rollback = "restored" if initial["active"] else "selection_restored"
            except (LifecycleError, OSError):
                rollback = "failed"
            raise LifecycleError("switch_failed" if allow_switch else "start_failed",
                                 "Target startup failed; inspect rollback outcome before retrying.",
                                 {"cause": cause, "rollback": rollback, "previous_world_id": previous}) from error
        return dict(result, operation=operation, changed=True)

    def start_world(self, world_id):
        with self._lock():
            return self._change(world_id, False)

    def switch_world(self, world_id):
        with self._lock():
            return self._change(world_id, True)
