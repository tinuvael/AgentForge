"""Independent native filesystem adversary with an explicit pipe barrier."""

import json
import sys
from pathlib import Path

target, outside = map(Path, sys.argv[2:])
moved = target.with_name(target.name + ".moved")
target.rename(moved)
try:
    target.symlink_to(outside, target_is_directory=sys.argv[1] == "directory")
    print(json.dumps({"event": "replaced"}), flush=True)
    sys.stdin.readline()
finally:
    target.unlink()
    moved.rename(target)
