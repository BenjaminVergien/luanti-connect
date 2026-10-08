#!/usr/bin/env python3
"""Install the reviewed, narrowly privileged world controller (run once as root)."""

import hashlib
import fcntl
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import tempfile
import time


SERVICE = "luanti-dev.service"
LIB = Path("/usr/local/lib/civicrm-worldctl")
CONFIG = Path("/etc/civicrm-worldctl/config.json")
STATE = Path("/var/lib/civicrm-worldctl/state.json")
MANIFEST = STATE.parent / "install-manifest.json"
WRAPPER = Path("/usr/local/sbin/civicrm-worldctl")
DROPIN = Path("/etc/systemd/system/luanti-dev.service.d/civicrm-worldctl.conf")
SUDOERS = Path("/etc/sudoers.d/civicrm-worldctl")
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C"}
CONFIGURATION = {
    "service": SERVICE,
    "engine": "/usr/bin/luanti",
    "game_id": "minetest",
    "user_home": "/home/ben",
    "server_config": "/home/ben/luanti-dev/server.conf",
    "logfile": "/home/ben/luanti-dev/server.log",
    "worlds": {
        "dev": "/home/ben/luanti-dev/world",
        "test": "/home/ben/luanti-dev/worlds/test",
    },
}


def run(*arguments):
    return subprocess.run(arguments, check=True, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=ENV).stdout.strip()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def check_path(path):
    """Reject symlink components, including dangling leaf links."""
    for component in reversed((path, *path.parents)):
        if component.is_symlink():
            raise RuntimeError(f"Refusing symlink: {component}")


def root_directory(path):
    check_path(path)
    if not path.exists():
        root_directory(path.parent)
        path.mkdir(mode=0o755)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError(f"Expected a root-owned, non-writable directory: {path}")


def atomic_write(path, content, mode):
    check_path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".worldctl-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), mode)
            os.fchown(target.fileno(), 0, 0)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def secure_existing(path):
    check_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError(f"Expected a root-owned, non-writable regular file: {path}")


def expected_service(reinstall):
    if run("/usr/bin/systemctl", "show", SERVICE, "--property=User", "--value") != "ben":
        raise RuntimeError("The existing service must run as ben")
    command = run("/usr/bin/systemctl", "show", SERVICE, "--property=ExecStart", "--value")
    if reinstall:
        if str(LIB / "launch.py") not in command:
            raise RuntimeError("Installed service no longer uses the managed launcher")
    else:
        import shlex
        if "argv[]=" not in command:
            raise RuntimeError("Cannot determine existing service command")
        arguments = shlex.split(command.split("argv[]=", 1)[1].split(" ;", 1)[0])
        worlds = [a.split("=", 1)[1] for a in arguments if a.startswith("--world=")]
        worlds.extend(arguments[i + 1] for i, value in enumerate(arguments[:-1]) if value == "--world")
        if worlds != [CONFIGURATION["worlds"]["dev"]]:
            raise RuntimeError("Initial installation requires the service to target the dev world")
        pid = int(run("/usr/bin/systemctl", "show", SERVICE, "--property=MainPID", "--value") or "0")
        if pid:
            arguments = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
            active_worlds = [a.split("=", 1)[1] for a in arguments if a.startswith("--world=")]
            active_worlds.extend(arguments[i + 1] for i, value in enumerate(arguments[:-1]) if value == "--world")
            if active_worlds != [CONFIGURATION["worlds"]["dev"]]:
                raise RuntimeError("Running process does not target the dev world")


def prepare_worlds():
    """Prepare only held directories beneath the exact development tree.

    No privileged chmod/chown/ACL operation resolves a writable pathname.
    Existing directories retain ownership, including developer-created ones.
    """
    owner = pwd.getpwnam("ben")
    developer = pwd.getpwnam("codex_civicrmbridge")
    approved_owners = {owner.pw_uid, developer.pw_uid}
    acl = "u::rwx,u:ben:rwx,u:codex_civicrmbridge:rwx,g::---,m::rwx,o::---"
    default_acl = ",".join("d:" + entry for entry in acl.split(","))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    held = []

    def hold(descriptor):
        held.append(descriptor)
        return descriptor

    def child(parent, name, create=False, private=False):
        created = False
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
                created = True
            except FileExistsError:
                pass
        descriptor = hold(os.open(name, directory_flags, dir_fd=parent))
        info = os.fstat(descriptor)
        if created:
            # A replacement created by a writable-directory owner is rejected
            # before changing ownership; newly created directories belong to root.
            if info.st_uid != 0:
                raise RuntimeError(f"New directory was replaced during preparation: {name}")
            os.fchown(descriptor, owner.pw_uid, owner.pw_gid)
        elif info.st_uid not in approved_owners:
            raise RuntimeError(f"Expected directory owned by ben or codex_civicrmbridge: {name}")
        if private:
            subprocess.run(
                ["/usr/bin/setfacl", "--set=" + acl + "," + default_acl,
                 f"/proc/self/fd/{descriptor}"],
                check=True, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, env=ENV, pass_fds=(descriptor,))
        return descriptor

    try:
        base = hold(os.open("/", directory_flags))
        for component in ("home", "ben", "luanti-dev"):
            base = hold(os.open(component, directory_flags, dir_fd=base))
            if os.fstat(base).st_uid not in ({0} | approved_owners):
                raise RuntimeError("Unexpected owner in development directory ancestry")
        dev = child(base, "world")
        worlds = child(base, "worlds", create=True, private=True)
        test = child(worlds, "test", create=True, private=True)
        try:
            descriptor = os.open("world.mt", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o660, dir_fd=test)
        except FileExistsError:
            descriptor = os.open("world.mt", os.O_RDONLY | os.O_NOFOLLOW |
                                 os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=test)
            with os.fdopen(descriptor, "rb") as existing:
                info = os.fstat(existing.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid not in approved_owners:
                    raise RuntimeError("Existing world.mt must be a regular file owned by an approved account")
                raw = existing.read(65537)
                if len(raw) > 65536:
                    raise RuntimeError("Existing world.mt exceeds 64 KiB")
            settings = dict(line.split("=", 1) for line in raw.decode().splitlines()
                            if "=" in line and not line.lstrip().startswith("#"))
            settings = {key.strip(): value.strip() for key, value in settings.items()}
            if settings.get("gameid") != "minetest" or settings.get("load_mod_civicrm_bridge") != "mods/civicrm_bridge":
                raise RuntimeError("Existing test world configuration does not enable the shared bridge")
        else:
            with os.fdopen(descriptor, "w") as output:
                output.write("gameid = minetest\nbackend = sqlite3\nplayer_backend = sqlite3\n"
                             "auth_backend = sqlite3\nmod_storage_backend = sqlite3\n"
                             "load_mod_civicrm_bridge = mods/civicrm_bridge\n")
                output.flush()
                os.fsync(output.fileno())
                os.fchown(output.fileno(), owner.pw_uid, owner.pw_gid)
        for world in (dev, test):
            runtime = child(world, "civicrm_bridge", create=True, private=True)
            for name in ("inbox", "outbox", "receipts"):
                child(runtime, name, create=True, private=True)
    finally:
        for descriptor in reversed(held):
            os.close(descriptor)


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run this reviewed installer with sudo as ben")
    os.umask(0o022)
    root_directory(Path("/run"))
    lock_descriptor = os.open("/run/civicrm-worldctl.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    lock_info = os.fstat(lock_descriptor)
    if lock_info.st_uid != 0 or not stat.S_ISREG(lock_info.st_mode) or lock_info.st_mode & 0o022:
        os.close(lock_descriptor)
        raise RuntimeError("Expected a root-owned lifecycle lock file")
    # Held until this standalone installer exits, serializing with worldctl.
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    for required in ("/usr/sbin/visudo", "/usr/bin/setfacl", "/usr/bin/luanti", "/usr/bin/python3"):
        if not Path(required).is_file():
            raise RuntimeError(f"Required executable not found: {required}")
    source = Path(__file__).resolve().parent
    files = {}
    for name in ("controller.py", "worldctl.py", "launch.py"):
        content = (source / name).read_bytes()
        compile(content, name, "exec")  # Validate syntax without executing source.
        files[LIB / name] = (content, 0o644)
    files[CONFIG] = (json_bytes(CONFIGURATION), 0o644)
    files[WRAPPER] = (b'#!/bin/sh\nexec /usr/bin/python3 -I /usr/local/lib/civicrm-worldctl/worldctl.py "$@"\n', 0o755)
    files[DROPIN] = (b"[Service]\nExecStart=\nExecStart=/usr/bin/python3 -I /usr/local/lib/civicrm-worldctl/launch.py\nTimeoutStopSec=30s\n", 0o644)
    commands = ["status", "stop_world", "start_world dev", "start_world test", "switch_world dev", "switch_world test"]
    sudoers = "# Managed by civicrm-worldctl installer; exact commands only.\n"
    sudoers += "codex_civicrmbridge ALL=(root) NOPASSWD: " + ", ".join(str(WRAPPER) + " " + command for command in commands) + "\n"
    files[SUDOERS] = (sudoers.encode(), 0o440)
    check_path(MANIFEST)
    reinstall = MANIFEST.exists()
    previous = {}
    if reinstall:
        secure_existing(MANIFEST)
        previous = json.loads(MANIFEST.read_text())["sha256"]
    for path in files:
        check_path(path)
        if path.exists():
            secure_existing(path)
            if not reinstall or hashlib.sha256(path.read_bytes()).hexdigest() != previous.get(str(path)):
                raise RuntimeError(f"Refusing to overwrite unrecognized or locally modified file: {path}")
        elif reinstall:
            raise RuntimeError(f"Managed installation file is missing: {path}")
    check_path(STATE)
    if STATE.exists():
        secure_existing(STATE)
        if not reinstall or json.loads(STATE.read_text()).get("world_id") not in CONFIGURATION["worlds"]:
            raise RuntimeError("Unrecognized state file; refusing to reset selected world")
    elif reinstall:
        raise RuntimeError("State file is missing; refusing to reset selected world")
    expected_service(reinstall)
    with tempfile.TemporaryDirectory(prefix="civicrm-worldctl-") as temporary:
        candidate = Path(temporary) / "sudoers"
        candidate.write_bytes(files[SUDOERS][0])
        candidate.chmod(0o440)
        run("/usr/sbin/visudo", "-cf", str(candidate))
    prepare_worlds()
    for directory in (LIB, CONFIG.parent, STATE.parent, WRAPPER.parent, DROPIN.parent, SUDOERS.parent):
        root_directory(directory)
    backups = STATE.parent / ("backup-" + time.strftime("%Y%m%dT%H%M%S") + "-" + str(os.getpid()))
    backups.mkdir(mode=0o700)
    originals = {}
    for path in files:
        originals[path] = (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
        if originals[path]:
            name = str(path).lstrip("/").replace("/", "__")
            atomic_write(backups / name, originals[path][0], originals[path][1])
    atomic_write(backups / "manifest.json", json_bytes({str(path): value is not None for path, value in originals.items()}), 0o600)
    changed = []
    new_state = not STATE.exists()
    try:
        for path, (content, mode) in files.items():
            atomic_write(path, content, mode)
            changed.append(path)
        if new_state:
            atomic_write(STATE, json_bytes({"world_id": "dev"}), 0o644)
        run("/usr/sbin/visudo", "-cf", "/etc/sudoers")
        run("/usr/bin/systemctl", "daemon-reload")
        run("/usr/bin/systemd-analyze", "verify", SERVICE)
        expected_service(True)
        manifest = {"version": 1, "sha256": {str(path): hashlib.sha256(content).hexdigest() for path, (content, _) in files.items()}}
        atomic_write(MANIFEST, json_bytes(manifest), 0o644)
    except Exception:
        for path in reversed(changed):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, *original)
        if new_state:
            STATE.unlink(missing_ok=True)
        subprocess.run(["/usr/bin/systemctl", "daemon-reload"], env=ENV, check=False)
        raise
    print("Installed and verified. The service was not restarted.")
    print(f"Managed-file backups: {backups}")
    print("From the codex_civicrmbridge session:")
    print("  sudo -n /usr/local/sbin/civicrm-worldctl status")
    print("  sudo -n /usr/local/sbin/civicrm-worldctl switch_world test")
    print("  sudo -n /usr/local/sbin/civicrm-worldctl switch_world dev")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Installation failed: {error}")
