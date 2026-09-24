"""Publish validated data without overwriting concurrent human or collector changes."""
import argparse
import subprocess
from pathlib import Path

DATA_FILES = ('land_tracker_dashboard_20260614/index.html',
              'land_tracker_dashboard_20260614/feishu-feed.json',
              'data/land_tracker_sources.json', 'data/land_tracker_enrichment.json')


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], text=True,
                          capture_output=True, check=check)


def protected(path):
    return (path in DATA_FILES or path == 'data/land_tracker_identity_sources.json'
            or path == '.github/workflows/land-tracker.yml'
            or path.startswith('scripts/'))


def publish(root):
    root = Path(root)
    base = git(root, 'rev-parse', 'HEAD').stdout.strip()
    staged = git(root, 'diff', '--cached', '--name-only').stdout.splitlines()
    unstaged = git(root, 'diff', '--name-only').stdout.splitlines()
    if any(path not in DATA_FILES for path in staged + unstaged):
        raise RuntimeError('Unrelated local edits present; use a clean checkout and rerun')
    git(root, 'add', '--', *DATA_FILES)
    if git(root, 'diff', '--cached', '--quiet', check=False).returncode == 0:
        return
    git(root, 'commit', '-m', 'Update official Beijing land transactions')
    for attempt in range(3):
        git(root, 'fetch', 'origin', 'main')
        remote = git(root, 'rev-parse', 'origin/main').stdout.strip()
        if remote != base:
            if git(root, 'merge-base', '--is-ancestor', base, remote, check=False).returncode:
                raise RuntimeError('Remote history changed; rerun collection from main')
            changed = git(root, 'diff', '--name-only', base, remote).stdout.splitlines()
            if any(protected(path) for path in changed):
                raise RuntimeError('Remote data or collector changed; rerun collection from main')
            result = git(root, 'rebase', 'origin/main', check=False)
            if result.returncode:
                git(root, 'rebase', '--abort', check=False)
                raise RuntimeError('Cannot safely rebase generated data; rerun collection')
            base = remote
        result = git(root, 'push', 'origin', 'HEAD:main', check=False)
        if result.returncode == 0:
            print(result.stderr.strip())
            return
        if attempt == 2:
            raise RuntimeError('Data push failed after three checked attempts: ' + result.stderr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    publish(parser.parse_args().root)
