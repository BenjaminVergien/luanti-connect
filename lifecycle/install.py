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


def prepare_worlds(base_path=Path("/home/ben/luanti-dev"), owner=None, developer=None, api_user=None):
    """Remove legacy developer writes, using only held no-follow descriptors.

    The only integration write boundary is civicrm_bridge/inbox. Existing
    developer-owned world objects become service-owned so chmod cannot undo it.
    """
    owner = owner or pwd.getpwnam("ben")
    developer = developer or pwd.getpwnam("codex_civicrmbridge")
    if api_user is None:
        try:
            api_user = pwd.getpwnam("civicrm_bridge_api")
        except KeyError:
            pass
    publishers = {developer.pw_uid}
    if api_user is not None:
        publishers.add(api_user.pw_uid)
    approved_owners = {0, owner.pw_uid, *publishers}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    held = []

    def hold(descriptor):
        held.append(descriptor)
        return descriptor

    def child(parent, name, create=False):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        return hold(os.open(name, flags | os.O_DIRECTORY, dir_fd=parent))

    def inspect(descriptor):
        info = os.fstat(descriptor)
        if info.st_uid not in approved_owners:
            raise RuntimeError("Unexpected world object owner")
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise RuntimeError("World objects must be regular files or directories")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise RuntimeError("Refusing hard-linked world file")
        return info

    def inspect_tree(descriptor):
        inspect(descriptor)
        for name in os.listdir(descriptor):
            try:
                entry = os.open(name, flags, dir_fd=descriptor)
            except FileNotFoundError:
                # The running service can finish an atomic write or remove a
                # consumed inbox file while the installer inspects the tree.
                continue
            try:
                info = inspect(entry)
                if stat.S_ISDIR(info.st_mode):
                    inspect_tree(entry)
            finally:
                os.close(entry)

    def secure(descriptor, permission="---", directory=False, inbox=False, api_permission=None):
        info = inspect(descriptor)
        if directory != stat.S_ISDIR(info.st_mode):
            raise RuntimeError("Unexpected world object type")
        os.fchown(descriptor, owner.pw_uid, owner.pw_gid)
        own = "rwx" if directory else "rw-"
        permissions = {uid: permission for uid in publishers}
        if api_user is not None and api_permission is not None:
            permissions[api_user.pw_uid] = api_permission
        grants = ",".join(f"u:{uid}:{permissions[uid]}" for uid in sorted(publishers))
        acl = f"u::{own},{grants},g::---,m::{permission},o::---"
        if inbox:
            # Submitted files belong to their publisher; inherit service access.
            grants = ",".join(f"u:{uid}:{own}" for uid in sorted(publishers | {owner.pw_uid}))
            acl = f"u::{own},{grants},g::---,m::{own},o::---"
        if directory:
            acl += "," + ",".join("d:" + entry for entry in acl.split(","))
        subprocess.run(["/usr/bin/setfacl", "--set=" + acl, f"/proc/self/fd/{descriptor}"],
                       check=True, text=True, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, env=ENV, pass_fds=(descriptor,))

    def secure_tree(descriptor, relative=()):
        info = inspect(descriptor)
        directory = stat.S_ISDIR(info.st_mode)
        inbox = relative[:2] == ("civicrm_bridge", "inbox")
        results = relative[:2] == ("civicrm_bridge", "outbox")
        permission = ("r-x" if directory else "r--") if results else "---"
        if directory and relative in ((), ("civicrm_bridge",)):
            permission = "--x"
        secure(descriptor, permission, directory, inbox)
        if directory:
            for name in os.listdir(descriptor):
                try:
                    entry = os.open(name, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    continue
                try:
                    secure_tree(entry, relative + (name,))
                finally:
                    os.close(entry)

    try:
        if not base_path.is_absolute() or base_path.resolve() != base_path:
            raise RuntimeError("Development tree must use an absolute, non-symlink path")
        base = hold(os.open("/", flags | os.O_DIRECTORY))
        for index, component in enumerate(base_path.parts[1:]):
            base = child(base, component)
            if os.fstat(base).st_uid not in {0, owner.pw_uid} and index < len(base_path.parts) - 2:
                raise RuntimeError("Unexpected owner in development directory ancestry")
        dev = child(base, "world")
        # Preflight existing objects before removing any legacy grants.
        inspect_tree(dev)
        worlds = child(base, "worlds", create=True)
        test = child(worlds, "test", create=True)
        inspect_tree(test)
        for world in (dev, test):
            try:
                mods = os.open("worldmods", flags | os.O_DIRECTORY, dir_fd=world)
            except FileNotFoundError:
                pass
            else:
                try:
                    if os.listdir(mods):
                        raise RuntimeError("Remove or review world-local mods before installation")
                finally:
                    os.close(mods)
        # Revoke replacement/creation access before touching world children.
        secure(base, "--x", directory=True)
        for parent, name, descriptor in ((base, "world", dev), (base, "worlds", worlds)):
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            opened = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise RuntimeError("World directory changed during permission preparation")
        secure(worlds, "--x", directory=True)
        current = os.stat("test", dir_fd=worlds, follow_symlinks=False)
        opened = os.fstat(test)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise RuntimeError("Test directory changed during permission preparation")
        for world in (dev, test):
            secure(world, "--x", directory=True)
        try:
            descriptor = os.open("world.mt", os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=test)
        except FileExistsError:
            descriptor = os.open("world.mt", flags, dir_fd=test)
            with os.fdopen(descriptor, "rb") as existing:
                inspect(existing.fileno())
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
        for world in (dev, test):
            runtime = child(world, "civicrm_bridge", create=True)
            for name in ("inbox", "outbox", "receipts"):
                child(runtime, name, create=True)
            secure_tree(world)
        for name in ("server.conf", "server.log"):
            try:
                entry = os.open(name, flags, dir_fd=base)
            except FileNotFoundError:
                if name == "server.conf":
                    raise
                continue
            try:
                secure(entry, "r--" if name == "server.log" else "---", api_permission="---")
            finally:
                os.close(entry)
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
    for required in ("/usr/sbin/visudo", "/usr/bin/setfacl", "/usr/bin/getfacl", "/usr/bin/luanti", "/usr/bin/python3"):
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
    for directory in (LIB, CONFIG.parent, STATE.parent, WRAPPER.parent, DROPIN.parent, SUDOERS.parent):
        root_directory(directory)
    backups = STATE.parent / ("backup-" + time.strftime("%Y%m%dT%H%M%S") + "-" + str(os.getpid()))
    backups.mkdir(mode=0o700)
    # Permission repairs intentionally remain restrictive on managed-file failure;
    # retain the previous ACLs for administrator-directed recovery, never reopen
    # the vulnerable world automatically.
    base = Path("/home/ben/luanti-dev")
    ancestors = [base, base / "worlds", Path(CONFIGURATION["server_config"]),
                 Path(CONFIGURATION["logfile"])]
    permissions = run("/usr/bin/getfacl", "-p", *[str(p) for p in ancestors if p.exists()])
    for world in CONFIGURATION["worlds"].values():
        if Path(world).exists():
            permissions += "\n\n" + run("/usr/bin/getfacl", "-R", "-P", "-p", world)
    atomic_write(backups / "permissions.acl", (permissions + "\n").encode(), 0o600)
    prepare_worlds()
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
