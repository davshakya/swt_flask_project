from __future__ import annotations

import re
from typing import Any


_READ_ONLY_STATEMENT = re.compile(
    r"^\s*(SELECT|SHOW|DESCRIBE|DESC|EXPLAIN)\b",
    re.IGNORECASE,
)
_LOCKING_SELECT = re.compile(
    r"\bFOR\s+(UPDATE|SHARE)\b|\bLOCK\s+IN\s+SHARE\s+MODE\b",
    re.IGNORECASE,
)


def statement_allows_connection_retry(sql: Any) -> bool:
    """Return whether replaying a statement after connection loss is safe.

    MySQL may apply a write before the client observes a connection failure.
    Replaying arbitrary writes can therefore duplicate side effects, so only
    plain, non-locking reads are eligible for an automatic reconnect retry.
    """

    statement = str(sql or "")
    if not _READ_ONLY_STATEMENT.match(statement):
        return False
    return _LOCKING_SELECT.search(statement) is None
