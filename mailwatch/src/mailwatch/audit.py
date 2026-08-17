"""Audit logging — structured JSON-lines record of every classification + action.

Each event is a single JSON object appended to ``<data_dir>/audit.jsonl``.
The log is append-only and safe to tail/rotate externally.  Fields follow
the issue's requirement: account, folder, uid, score, action.

Standard event types::

    classification   message classified; may carry ``action``
    move             message moved to Junk
    sieve_block      auto-block Sieve rule pushed
    train            classifier trained (spam/ham sample)
    feed_update      phishing feed refresh result
    startup/shutdown daemon lifecycle
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLog:
    """Thread-safe JSON-lines audit logger."""

    def __init__(
        self, path: str | Path | None = None, *, dry_run: bool = False
    ) -> None:
        if path is None:
            from mailwatch.paths import data_dir

            path = data_dir() / "audit.jsonl"
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dry_run = dry_run
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        """Append one audit event line."""
        record: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "event": event}
        if self.dry_run:
            record["dry_run"] = True
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError as exc:
                logger.warning("Failed to write audit log %s: %s", self.path, exc)

    def classification(
        self,
        account: str,
        folder: str,
        uid: int | None,
        message_id: str,
        from_addr: str,
        subject: str,
        scores: dict[str, float],
        is_spam: bool,
        is_phishing: bool,
        reasons: list[str],
        action: str,
    ) -> None:
        """Log a classification event with its resulting action."""
        self.emit(
            "classification",
            account=account,
            folder=folder,
            uid=uid,
            message_id=message_id,
            from_addr=from_addr,
            subject=subject[:200],
            scores=scores,
            is_spam=is_spam,
            is_phishing=is_phishing,
            reasons=reasons,
            action=action,
        )


__all__ = ["AuditLog"]
