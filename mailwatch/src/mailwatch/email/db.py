"""Database shim — mailwatch's SQLite wrapper re-exported under the
vendored ``email`` namespace so ported lighterbird tests import
``mailwatch.email.db.get_db`` unchanged.
"""

from mailwatch.db import MailwatchDB, get_db

__all__ = ["MailwatchDB", "get_db"]
