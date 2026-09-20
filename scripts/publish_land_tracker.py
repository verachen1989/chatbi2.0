"""Request a Pages rebuild using the job token and verify the public result."""
import hashlib
import os
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = "https://verachen1989.github.io/chatbi2.0/land_tracker_dashboard_20260614/"


def main():
    token = os.environ["GH_TOKEN"]
    api = requests.Session()
    api.headers.update({"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    endpoint = "https://api.github.com/repos/verachen1989/chatbi2.0/pages/builds"
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    response = api.post(endpoint, timeout=30)
    response.raise_for_status()
    expected = {
        filename: hashlib.sha256((ROOT / "land_tracker_dashboard_20260614" / filename).read_bytes()).hexdigest()
        for filename in ("index.html", "feishu-feed.json")
    }
    for _ in range(40):
        status = api.get(endpoint + "/latest", timeout=30)
        status.raise_for_status()
        result = status.json()
        print(f"Pages build: {result.get('status')}", flush=True)
        if result.get("status") == "errored":
            raise RuntimeError(f"Pages build failed: {result.get('error')}")
        if result.get("status") == "built" and result.get("commit") == revision:
            # The public request deliberately uses a separate, unauthenticated client.
            matches = []
            for filename, digest in expected.items():
                page = requests.get(PUBLIC + filename, params={"v": revision}, timeout=30)
                page.raise_for_status()
                matches.append(hashlib.sha256(page.content).hexdigest() == digest)
            if all(matches):
                print("Public page and Feishu feed verified: " + PUBLIC)
                return
        time.sleep(10)
    raise RuntimeError("Pages build/public HTML verification timed out; repository data is retained")


if __name__ == "__main__":
    main()
