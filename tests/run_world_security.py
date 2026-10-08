#!/usr/bin/env python3
"""Exercise worldmod and effective-ACL boundaries on disposable Linux worlds."""

import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from lifecycle.controller import LifecycleController, LifecycleError, check_trusted_permissions
from lifecycle.install import prepare_worlds
from run_lifecycle import create_fixture, NeverReadyBackend


class WorldSecurityTests(unittest.TestCase):
    def setUp(self):
        self.artifacts, self.config, self.state, self.lock = create_fixture(
            Path.home(), "/usr/bin/luanti", Path("/home/ben/.minetest/games/minetest_game"))
        self.addCleanup(shutil.rmtree, self.artifacts)
        self.world = Path(self.config["worlds"]["test"])
        self.settings = self.world / "world.mt"
        self.outsider = pwd.getpwnam("nobody")

    def acl(self, target, specification):
        subprocess.run(["/usr/bin/setfacl", "-m", specification, str(target)], check=True)

    def assert_rejected_before_mutation(self):
        for active in (False, True):
            backend = NeverReadyBackend(self.config, self.state)
            backend.active = active
            backend.world = self.config["worlds"]["dev"] if active else None
            controller = LifecycleController(self.config, backend, self.state, self.lock,
                                             timeout=.02, poll_interval=.001)
            with self.assertRaises(LifecycleError) as failure:
                (controller.switch_world if active else controller.start_world)("test")
            self.assertEqual(failure.exception.code, "invalid_world")
            self.assertEqual((backend.starts, backend.stops), (0, 0))
            self.assertIn('"world_id": "dev"', self.state.read_text())

    def validate(self):
        controller = LifecycleController(self.config, None, self.state, self.lock)
        return controller.validate_world("test")

    def test_arbitrary_mod_rejected_even_when_disabled(self):
        mod = self.world / "worldmods" / "unexpected"
        mod.mkdir()
        (mod / "init.lua").write_text("-- Must never execute.\n")
        self.assert_rejected_before_mutation()
        self.settings.write_text(self.settings.read_text() + "load_mod_unexpected = true\n")
        self.assert_rejected_before_mutation()

    def test_dangling_worldmods_symlink_rejected(self):
        mods = self.world / "worldmods"
        mods.rmdir()
        mods.symlink_to(self.artifacts / "missing", target_is_directory=True)
        self.assert_rejected_before_mutation()

    def test_world_settings_symlink_rejected(self):
        copy = self.artifacts / "alternate.mt"
        self.settings.rename(copy)
        self.settings.symlink_to(copy)
        self.assert_rejected_before_mutation()

    def test_acl_write_on_world_settings_rejected(self):
        self.acl(self.settings, f"u:{self.outsider.pw_uid}:rw-")
        self.assert_rejected_before_mutation()

    def test_acl_write_on_world_root_rejected(self):
        self.acl(self.world, f"u:{self.outsider.pw_uid}:rwx")
        self.assert_rejected_before_mutation()

    def test_acl_write_on_parent_rejected(self):
        self.acl(self.artifacts, f"u:{self.outsider.pw_uid}:rwx")
        self.assert_rejected_before_mutation()

    def test_writable_default_acl_rejected(self):
        self.acl(self.world, f"d:u:{self.outsider.pw_uid}:rwx")
        self.assert_rejected_before_mutation()

    def test_group_and_other_write_rejected(self):
        for mode in (0o660, 0o606):
            self.settings.chmod(mode)
            self.assert_rejected_before_mutation()

    def test_masked_named_write_does_not_grant_write(self):
        self.acl(self.world, f"u:{self.outsider.pw_uid}:rwx,m::r-x")
        self.assertEqual(self.validate(), self.world)

    def test_service_named_write_is_trusted(self):
        self.acl(self.world, f"u:{os.getuid()}:rwx,g::---")
        self.assertEqual(self.validate(), self.world)

    def test_untrusted_owner_rejected(self):
        descriptor = os.open(self.settings, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        metadata = os.fstat(descriptor)
        values = list(metadata)
        values[4] = self.outsider.pw_uid
        with patch("lifecycle.controller.os.fstat", return_value=os.stat_result(values)):
            with self.assertRaises(LifecycleError):
                check_trusted_permissions(descriptor, {0, os.getuid()})

    def installer_fixture(self):
        (self.artifacts / "dev").rename(self.artifacts / "world")
        (self.artifacts / "worlds").mkdir()
        self.world.rename(self.artifacts / "worlds" / "test")
        self.world = self.artifacts / "worlds" / "test"
        self.settings = self.world / "world.mt"
        self.config["worlds"] = {"dev": str(self.artifacts / "world"), "test": str(self.world)}
        runtime = self.world / "civicrm_bridge"
        for name in ("inbox", "outbox", "receipts"):
            (runtime / name).mkdir(parents=True, exist_ok=True)
            (runtime / name / "fixture.json").write_text('{}\n')
        for target in [self.artifacts, self.artifacts / "worlds", *self.artifacts.rglob("*")]:
            if target.is_symlink():
                continue
            permissions = "rwx" if target.is_dir() else "rw-"
            self.acl(target, f"u:{self.outsider.pw_uid}:{permissions}")
            if target.is_dir():
                self.acl(target, f"d:u:{self.outsider.pw_uid}:rwx")
        return runtime

    def prepare(self):
        prepare_worlds(self.artifacts, pwd.getpwuid(os.getuid()), self.outsider)

    def test_installer_repairs_existing_acl_and_preserves_data(self):
        runtime = self.installer_fixture()
        before = self.settings.read_bytes()
        self.prepare()
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertEqual(self.validate(), self.world)
        for target in (self.artifacts, self.world, self.settings,
                       runtime, runtime / "receipts", runtime / "receipts" / "fixture.json",
                       self.artifacts / "server.conf"):
            self.assertFalse(target.stat().st_mode & 0o022, str(target))
        results = subprocess.check_output(["getfacl", "-cp", str(runtime / "outbox" / "fixture.json")], text=True)
        self.assertIn("user:nobody:r--", results)
        self.assertEqual((runtime / "receipts" / "fixture.json").read_text(), '{}\n')
        inbox = subprocess.check_output(["getfacl", "-cp", str(runtime / "inbox")], text=True)
        self.assertIn("user:nobody:rwx", inbox)
        self.assertIn("default:user:nobody:rwx", inbox)
        # Both newly created settings and result files inherit safe ACLs.
        fresh = self.world / "fresh.mt"
        fresh.write_text("fixture\n")
        self.assertFalse(fresh.stat().st_mode & 0o022)
        fresh_result = runtime / "outbox" / "new.json"
        fresh_result.write_text('{}\n')
        self.assertFalse(fresh_result.stat().st_mode & 0o022)
        self.prepare()
        self.assertEqual(self.validate(), self.world)

    def test_installer_rejects_symlink_without_touching_target(self):
        self.installer_fixture()
        outside = self.artifacts / "outside"
        outside.write_text("unchanged\n")
        before = outside.stat()
        (self.world / "escape").symlink_to(outside)
        with self.assertRaises(OSError):
            self.prepare()
        self.assertEqual(outside.read_text(), "unchanged\n")
        self.assertEqual(outside.stat().st_mode, before.st_mode)

    def test_installer_rejects_hardlink_without_touching_target(self):
        self.installer_fixture()
        outside = self.artifacts / "outside"
        outside.write_text("unchanged\n")
        before = outside.stat()
        os.link(outside, self.world / "escape")
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(outside.stat().st_mode, before.st_mode)

    def test_installer_rejects_existing_worldmod(self):
        self.installer_fixture()
        (self.world / "worldmods" / "unexpected").mkdir()
        before = self.artifacts.stat().st_mode
        with self.assertRaises(RuntimeError):
            self.prepare()
        self.assertEqual(self.artifacts.stat().st_mode, before)

    def test_installer_creates_private_fresh_test_world(self):
        self.installer_fixture()
        shutil.rmtree(self.world)
        self.prepare()
        self.assertEqual(self.validate(), self.world)
        self.assertFalse(self.settings.stat().st_mode & 0o022)
        self.assertNotIn("bridge_test_driver", self.settings.read_text())

    def test_installer_rejects_replaced_world_directory(self):
        self.installer_fixture()
        original_run = subprocess.run
        replaced = False

        def replace_after_base_lock(arguments, **options):
            nonlocal replaced
            result = original_run(arguments, **options)
            if not replaced and arguments[0] == "/usr/bin/setfacl":
                replaced = True
                (self.artifacts / "world").rename(self.artifacts / "replaced-world")
                (self.artifacts / "world").mkdir()
            return result

        with patch("lifecycle.install.subprocess.run", side_effect=replace_after_base_lock):
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                self.prepare()


if __name__ == "__main__":
    unittest.main()
