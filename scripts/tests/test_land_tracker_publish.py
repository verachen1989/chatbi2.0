import importlib
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True, check=True)
    return result.stdout.strip()


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('commit_land_tracker'), 'Conflict-safe publisher is missing')
        self.module = importlib.import_module('commit_land_tracker')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.remote, self.bot, self.editor = [base / x for x in ('remote', 'bot', 'editor')]
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(self.remote)], capture_output=True, check=True)
        subprocess.run(['git', 'clone', str(self.remote), str(self.bot)], capture_output=True, check=True)
        self.configure(self.bot)
        for name in self.module.DATA_FILES:
            path = self.bot / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('original\n')
        (self.bot / 'README.md').write_text('original readme\n')
        git(self.bot, 'add', '.')
        git(self.bot, 'commit', '-m', 'initial')
        git(self.bot, 'push', 'origin', 'main')
        subprocess.run(['git', 'clone', str(self.remote), str(self.editor)], capture_output=True, check=True)
        self.configure(self.editor)
        (self.bot / self.module.DATA_FILES[0]).write_text('new verified data\n')

    def configure(self, path):
        git(path, 'config', 'user.name', 'Test')
        git(path, 'config', 'user.email', 'test@example.com')
        git(path, 'config', 'commit.gpgsign', 'false')

    def edit_remote(self, name):
        path = self.editor / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('newer human edit\n')
        git(self.editor, 'add', name)
        git(self.editor, 'commit', '-m', 'concurrent edit')
        git(self.editor, 'push', 'origin', 'main')

    def test_unrelated_remote_change_survives_rebase_and_push(self):
        self.edit_remote('README.md')
        self.module.publish(self.bot)
        self.assertEqual(git(self.remote, 'show', 'main:README.md'), 'newer human edit')
        self.assertEqual(git(self.remote, 'show', 'main:' + self.module.DATA_FILES[0]), 'new verified data')

    def test_concurrent_generated_data_change_stops_without_overwriting(self):
        self.edit_remote(self.module.DATA_FILES[0])
        revision = git(self.remote, 'rev-parse', 'main')
        with self.assertRaisesRegex(RuntimeError, 'rerun'):
            self.module.publish(self.bot)
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), revision)
        self.assertEqual(git(self.remote, 'show', 'main:' + self.module.DATA_FILES[0]), 'newer human edit')

    def test_concurrent_collector_change_requires_new_collection(self):
        self.edit_remote('scripts/update_land_tracker.py')
        revision = git(self.remote, 'rev-parse', 'main')
        with self.assertRaisesRegex(RuntimeError, 'rerun'):
            self.module.publish(self.bot)
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), revision)

    def test_push_race_refetches_and_checks_again(self):
        original, pushed = self.module.git, []
        def racing_git(root, *args, **kwargs):
            if args[0] == 'push' and not pushed:
                pushed.append(True)
                self.edit_remote('README.md')
            return original(root, *args, **kwargs)
        with patch.object(self.module, 'git', side_effect=racing_git):
            self.module.publish(self.bot)
        self.assertEqual(git(self.remote, 'show', 'main:README.md'), 'newer human edit')
        self.assertEqual(git(self.remote, 'show', 'main:' + self.module.DATA_FILES[0]), 'new verified data')

    def test_unrelated_local_staged_changes_are_not_published(self):
        (self.bot / 'README.md').write_text('unrelated local work')
        git(self.bot, 'add', 'README.md')
        before = git(self.remote, 'rev-parse', 'main')
        with self.assertRaisesRegex(RuntimeError, 'Unrelated local edits'):
            self.module.publish(self.bot)
        self.assertEqual(git(self.remote, 'rev-parse', 'main'), before)


if __name__ == '__main__':
    unittest.main()
