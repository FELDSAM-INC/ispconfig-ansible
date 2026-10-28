"""Ownership migration regression tests; run in a disposable root container."""
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('migration', ROOT / 'roles/ispconfig_panel/files/migrate-runtime-owner.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@unittest.skipUnless(os.geteuid() == 0 and os.environ.get('ISPC_REST_TEST_CONTAINER') == '1', 'disposable root container only')
class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='rest-owner-', dir='/opt'))
        self.addCleanup(shutil.rmtree, self.base)
        self.tree = self.base / 'api'
        self.tree.mkdir()
        (self.tree / 'storage').mkdir()
        self.secret = self.tree / '.env'
        self.secret.write_text('APP_KEY=synthetic-key\n')
        self.secret.chmod(0o600)
        for p in [self.tree, self.tree / 'storage', self.secret]:
            os.chown(p, 48001, 48001)
        users = {'legacy': SimpleNamespace(pw_uid=48001, pw_gid=48001),
                 'api': SimpleNamespace(pw_uid=48002, pw_gid=48002),
                 'root': SimpleNamespace(pw_uid=0, pw_gid=0)}
        mock = patch.object(migration.pwd, 'getpwnam', side_effect=users.__getitem__)
        mock.start()
        self.addCleanup(mock.stop)

    def run_migration(self):
        migration.migrate(str(self.tree), 'legacy', 'api')

    def test_preserves_environment_and_permissions_and_can_resume(self):
        os.chown(self.tree / 'storage', 48002, 48002)
        self.run_migration()
        self.run_migration()
        for p in [self.tree, self.tree / 'storage', self.secret]:
            self.assertEqual(p.stat().st_uid, 48002)
        self.assertEqual(self.secret.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.secret.read_text(), 'APP_KEY=synthetic-key\n')

    def test_links_never_change_outside_file_or_directory_ownership(self):
        victim = self.base / 'victim'
        victim.mkdir()
        (victim / 'secret').write_text('unchanged')
        (self.tree / 'directory-link').symlink_to(victim)
        (self.tree / 'file-link').symlink_to(victim / 'secret')
        self.run_migration()
        self.assertEqual(victim.stat().st_uid, 0)
        self.assertEqual((victim / 'secret').stat().st_uid, 0)
        self.assertEqual((victim / 'secret').read_text(), 'unchanged')

    def test_hard_link_fails_before_any_ownership_changes(self):
        os.link(self.secret, self.base / 'outside-link')
        with self.assertRaises(ValueError): self.run_migration()
        self.assertEqual(self.secret.stat().st_uid, 48001)
        self.assertEqual(self.tree.stat().st_uid, 48001)

    def test_foreign_owner_fails_without_reassigning_it(self):
        os.chown(self.secret, 0, 0)
        with self.assertRaises(ValueError): self.run_migration()
        self.assertEqual(self.secret.stat().st_uid, 0)
        self.assertEqual(self.tree.stat().st_uid, 48001)

    def test_fifo_fails_without_blocking(self):
        fifo = self.tree / 'fifo'
        os.mkfifo(fifo)
        os.chown(fifo, 48001, 48001)
        with self.assertRaises(ValueError): self.run_migration()

    def test_symlink_install_directory_fails(self):
        link = self.base / 'alias'
        link.symlink_to(self.tree)
        with self.assertRaises(OSError): migration.migrate(str(link), 'legacy', 'api')

    def test_writable_parent_fails(self):
        self.base.chmod(0o777)
        with self.assertRaises(ValueError): self.run_migration()

    def test_root_runtime_is_rejected(self):
        with self.assertRaises(ValueError): migration.migrate(str(self.tree), 'legacy', 'root')


if __name__ == '__main__': unittest.main()
