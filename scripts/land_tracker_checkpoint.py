"""Short-lived checkpoints for complete, validated official-source queries."""
import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')


def now():
    return datetime.now(TZ)


class CheckpointStore:
    def __init__(self, directory, clock=None):
        self.directory = Path(directory)
        self.clock = clock or (lambda: now())
        self.pending = {}

    def path(self, key):
        if not re.fullmatch(r'[a-f0-9]{64}', key):
            raise ValueError('Invalid checkpoint key')
        return self.directory / (key + '.json')

    def load(self, key):
        path = self.path(key)
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
            observed = datetime.fromisoformat(item['fetchedAt'])
            current = self.clock()
            if (item['schemaVersion'] != 1 or observed.tzinfo is None
                    or not timedelta(0) <= current - observed <= timedelta(hours=6)
                    or observed.astimezone(TZ).date() != current.astimezone(TZ).date()
                    or hashlib.sha256(item['text'].encode()).hexdigest() != item['sha256']):
                return None
            return item['text'], observed
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def stage(self, key, text, observed_at):
        self.path(key)
        self.pending[key] = dict(schemaVersion=1, text=text,
                                 fetchedAt=observed_at.isoformat(),
                                 sha256=hashlib.sha256(text.encode()).hexdigest())

    def finish(self, success):
        pending, self.pending = self.pending, {}
        if success and pending:
            self.directory.mkdir(parents=True, exist_ok=True)
        for key, item in pending.items():
            path = self.path(key)
            if success:
                temporary = path.with_suffix('.tmp')
                temporary.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
                temporary.replace(path)
            else:
                path.unlink(missing_ok=True)
