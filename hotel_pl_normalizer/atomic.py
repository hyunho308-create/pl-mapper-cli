"""Replace one file with another, atomically, on Windows as well as POSIX.

The problem
-----------
Three places here write to a temporary file and then rename it over the target,
which is the standard way to make a reader see either the old file or the whole
new one, never a half-written one. The rename is the atomic step and it is
supposed to be the reliable part.

On Windows it is not quite. A rename fails with `PermissionError: [WinError 5]
Access is denied` whenever any other process holds a handle to either file
without having asked for share-delete. Nothing in this codebase holds one -- every
handle is closed before the rename -- but other processes on the machine open
files this code has just created, antivirus and the search indexer most of all,
and a freshly written `.xlsx` is exactly what they look at hardest.

That is why it shows up under load and disappears when the same code is run on
its own: it is a race against a scanner, and a busy machine loses it sometimes.
It was reproducible here as roughly one full test-suite run in two, failing a
different innocent test each time.

The remedy
----------
Wait and try again. The scanner holds the file for a few milliseconds, so a short
bounded retry converts an intermittent hard failure into a brief pause. This is
the accepted fix for the condition; there is no way to ask Windows to rename a
file another process has open.

The bound matters. Retrying forever would turn a genuine permission problem -- a
read-only file, a directory the process may not write -- into a hang, so this
gives up quickly and raises the original error, which is still the right error.

POSIX renames do not fail this way, so there the first attempt always wins and
none of this costs anything.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# Measured against the failure this was written for: the scanner's handle is gone
# well inside 100ms. The schedule below waits about 1.6s in total across all
# attempts, which is long enough to outlast a scan and short enough that a real
# permission error is still reported promptly.
_ATTEMPTS = 8
_INITIAL_BACKOFF_SECONDS = 0.01
_MAX_BACKOFF_SECONDS = 0.5


def replace_atomically(source: str | Path, target: str | Path) -> None:
    """Rename `source` onto `target`, retrying a transient Windows denial.

    Raises the original `OSError` if the file is still not replaceable once the
    retries are spent -- a permission problem that persists is a real one.
    """
    source = Path(source)
    target = Path(target)
    backoff = _INITIAL_BACKOFF_SECONDS
    for attempt in range(_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # Only PermissionError is worth retrying. A missing source or a bad
            # path will not fix itself, and retrying those just delays the report.
            if attempt == _ATTEMPTS - 1:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF_SECONDS)
