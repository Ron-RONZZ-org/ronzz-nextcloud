# ruff: noqa: S112, B905, SIM102, RUF001
"""Spam similarity detection (vendored from lighterbird @ 99baa8ef). — MinHash near-duplicate + repeated content guard.

Uses MinHash signatures to detect email content that resembles known spam
messages, and SHA-256 content hashes to guard against repeated spamming.

Architecture:
    - ``SpamSimilarityDetector`` — stores/retrieves signatures, computes similarity
    - MinHash with 128 permutations on word-bigram shingles
    - Exact content hash (SHA-256) for duplicate detection
"""

from __future__ import annotations

import hashlib
import logging
import struct
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Number of MinHash permutations (signature length)
_NUM_PERM = 128

# Similarity thresholds
_SIMILAR_THRESHOLD = 0.4  # moderately similar → flag for review
_NEAR_DUPE_THRESHOLD = 0.7  # near-duplicate → strong spam signal

# Repeated content guard
_REPEATED_SAME_SENDER_MAX = 3  # same content from same sender > N → harassment
_REPEATED_DIFF_SENDER_MIN = 2  # same content from ≥ N different senders → campaign


def _normalize(text: str) -> str:
    """Normalize text for content hashing.

    Lowercases, collapses whitespace, strips leading/trailing space.

    Args:
        text: Raw text.

    Returns:
        Normalized string.
    """
    import re

    return re.sub(r"\s+", " ", text.lower().strip())


def _content_hash(text: str) -> str:
    """SHA-256 hash of normalized text for exact duplicate detection.

    Args:
        text: The email subject + body text.

    Returns:
        Hex digest of SHA-256.
    """
    normalized = _normalize(text)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _extract_domain(from_addr: str) -> str | None:
    """Extract sender domain from a From header.

    Args:
        from_addr: ``"Name <email@domain.com>"`` or ``"email@domain.com"``.

    Returns:
        Domain part (lowercase), or None if unparseable.
    """
    if not from_addr:
        return None
    import re as _re

    m = _re.search(r"(\S+@\S+)", from_addr)
    if m:
        email = m.group(1).strip(">").strip()
    else:
        email = from_addr.strip()
    if "@" in email:
        return email.split("@")[-1].lower().strip("> \t")
    return None


def _make_shingles(words: list[str]) -> list[str]:
    """Build word-bigram shingles from a list of tokens.

    Consecutive token pairs form shingles.  Single-token inputs produce
    a single shingle.

    Args:
        words: List of word tokens (as returned by ``_tokenize`` ).

    Returns:
        List of shingle strings (e.g. ``["hello world", "world test"]``).
    """
    if len(words) < 2:
        return words[:1] if words else []
    return [" ".join(words[i : i + 2]) for i in range(len(words) - 1)]


def _minhash_signature(shingles: list[str], num_perm: int = _NUM_PERM) -> bytes:
    """Compute MinHash signature from shingles.

    Uses md5-based permutations.  Each permutation position stores the
    minimum hash value observed across all shingles.

    Args:
        shingles: List of shingle strings.
        num_perm: Number of permutation hash functions.

    Returns:
        ``4 * num_perm`` bytes of uint32 values (little-endian).
    """
    if not shingles:
        return struct.pack(f"<{num_perm}I", *([0] * num_perm))

    sig = [0xFFFFFFFF] * num_perm
    for shingle in shingles:
        h = int(hashlib.md5(shingle.encode()).hexdigest()[:8], 16)
        for i in range(num_perm):
            # Mix hash with permutation index using golden ratio
            mixed = (h ^ ((i + 1) * 0x9E3779B9)) & 0xFFFFFFFF
            if mixed < sig[i]:
                sig[i] = mixed
    return struct.pack(f"<{num_perm}I", *sig)


def _jaccard_from_bytes(sig1: bytes, sig2: bytes, num_perm: int = _NUM_PERM) -> float:
    """Estimate Jaccard similarity from two serialised MinHash signatures.

    Args:
        sig1: First signature bytes (``4 * num_perm`` bytes LE uint32).
        sig2: Second signature bytes.
        num_perm: Number of permutations.

    Returns:
        Estimated Jaccard similarity in [0.0, 1.0].
    """
    fmt = f"<{num_perm}I"
    a = struct.unpack(fmt, sig1)
    b = struct.unpack(fmt, sig2)
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / num_perm


class SpamSimilarityDetector:
    """MinHash-based similarity detection for spam messages.

    Stores MinHash signatures of known spam messages and compares new
    messages against the corpus.  Also tracks exact content hashes for
    repeated-spam detection.

    Args:
        db: Database connection for signature/content-hash queries.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    # ── Public API ─────────────────────────────────────────────────────────

    def add_spam(
        self,
        message_uuid: str,
        subject: str,
        body: str,
        account_email: str,
        from_addr: str = "",
    ) -> None:
        """Store similarity data for a spam message.

        Computes and stores the MinHash signature and content hash.
        Also updates the content-hash tracking table.

        Args:
            message_uuid: UUID of the message.
            subject: Email subject.
            body: Email body (plaintext).
            account_email: Account email for per-user isolation.
            from_addr: From header value (used to derive the sender
                domain for repeated-spam tracking).  mailwatch adaptation:
                lighterbird looked this up from the ``messages`` table,
                which mailwatch does not have.
        """
        from mailwatch.email.filters.spam_detect import _tokenize

        text = f"{subject} {body}" if subject else (body or "")
        if not text.strip():
            return

        content_h = _content_hash(text)
        now = datetime.now(UTC).isoformat()

        # Compute and store MinHash signature
        words = _tokenize(text)
        shingles = _make_shingles(words)
        signature = _minhash_signature(shingles)

        self._db.execute(
            "INSERT OR REPLACE INTO spam_signatures "
            "(message_uuid, account_email, signature, content_hash, "
            " num_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (message_uuid, account_email, signature, content_h, len(words), now),
        )

        # Derive sender domain from the From header directly.
        sender_domain = _extract_domain(from_addr) or ""

        # Upsert content hash for repeated-spam tracking
        existing = self._db.execute_one(
            "SELECT occurrence_count FROM spam_content_hashes "
            "WHERE content_hash = ? AND sender_domain = ? AND account_email = ?",
            (content_h, sender_domain, account_email),
        )
        if existing:
            self._db.execute(
                "UPDATE spam_content_hashes "
                "SET occurrence_count = occurrence_count + 1, last_seen_at = ? "
                "WHERE content_hash = ? AND sender_domain = ? AND account_email = ?",
                (now, content_h, sender_domain, account_email),
            )
        else:
            subject_snippet = (subject or "")[:80]
            self._db.execute(
                "INSERT INTO spam_content_hashes "
                "(content_hash, sender_domain, account_email, "
                " first_seen_at, last_seen_at, occurrence_count, subject_snippet) "
                "VALUES (?, ?, ?, ?, ?, 1, ?)",
                (content_h, sender_domain, account_email, now, now, subject_snippet),
            )

    def remove_message(self, message_uuid: str) -> None:
        """Remove similarity data when a false positive is corrected.

        Args:
            message_uuid: UUID of the message to remove.
        """
        self._db.execute(
            "DELETE FROM spam_signatures WHERE message_uuid = ?",
            (message_uuid,),
        )

    def check_similarity(
        self,
        subject: str,
        body: str,
        account_email: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Check a message against stored spam signatures.

        Computes the MinHash signature for the input text and compares
        it against the N most recent stored spam signatures.

        Args:
            subject: Email subject.
            body: Email body (plaintext).
            account_email: If given, only compare against signatures
                for this account.  If None, compare against all.
            limit: Number of recent spam signatures to compare against.

        Returns:
            Dict with ``score``, ``matched_uuid``, ``is_near_duplicate``,
            and ``is_similar``.
        """
        from mailwatch.email.filters.spam_detect import _tokenize

        text = f"{subject} {body}" if subject else (body or "")
        if not text.strip():
            return {
                "score": 0.0,
                "matched_uuid": None,
                "is_near_duplicate": False,
                "is_similar": False,
            }

        words = _tokenize(text)
        shingles = _make_shingles(words)
        query_sig = _minhash_signature(shingles)

        # Load stored signatures (most recent first)
        if account_email:
            rows = self._db.execute(
                "SELECT message_uuid, signature FROM spam_signatures "
                "WHERE account_email = ? ORDER BY created_at DESC LIMIT ?",
                (account_email, limit),
            )
        else:
            rows = self._db.execute(
                "SELECT message_uuid, signature FROM spam_signatures "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        best_score = 0.0
        best_uuid = None

        for row in rows:
            sig = row["signature"]
            if sig is None or len(sig) < _NUM_PERM * 4:
                continue
            try:
                sim = _jaccard_from_bytes(query_sig, sig)
                if sim > best_score:
                    best_score = sim
                    best_uuid = row["message_uuid"]
                    if best_score >= _NEAR_DUPE_THRESHOLD:
                        break  # early exit — good enough
            except Exception:
                continue

        return {
            "score": round(best_score, 4),
            "matched_uuid": best_uuid,
            "is_near_duplicate": best_score >= _NEAR_DUPE_THRESHOLD,
            "is_similar": best_score >= _SIMILAR_THRESHOLD,
        }

    def check_repeated(
        self,
        subject: str,
        body: str,
        from_addr: str,
        account_email: str | None = None,
    ) -> dict[str, Any]:
        """Check for repeated spam patterns.

        Looks up the content hash and checks:
        - Has this exact content been seen before?
        - Has it been sent by multiple different senders?
        - Has the same sender sent identical content excessively?

        Args:
            subject: Email subject.
            body: Email body (plaintext).
            from_addr: From header to extract sender domain.
            account_email: Account email for per-user lookup.

        Returns:
            Dict with ``is_repeated``, ``occurrence_count``,
            ``unique_senders``, and ``reason``.
        """
        text = f"{subject} {body}" if subject else (body or "")
        if not text.strip() or not account_email:
            return {
                "is_repeated": False,
                "occurrence_count": 0,
                "unique_senders": 0,
                "reason": "",
            }

        content_h = _content_hash(text)
        sender_domain = _extract_domain(from_addr) or ""

        rows = list(
            self._db.execute(
                "SELECT sender_domain, occurrence_count FROM spam_content_hashes "
                "WHERE content_hash = ? AND account_email = ?",
                (content_h, account_email),
            )
        )

        if not rows:
            return {
                "is_repeated": False,
                "occurrence_count": 0,
                "unique_senders": 0,
                "reason": "",
            }

        total_occurrences = sum(r["occurrence_count"] for r in rows)
        unique_senders = len(rows)

        # Same-sender harassment: identical content from same sender too often
        if sender_domain:
            for r in rows:
                if r["sender_domain"] == sender_domain:
                    if r["occurrence_count"] >= _REPEATED_SAME_SENDER_MAX:
                        return {
                            "is_repeated": True,
                            "occurrence_count": total_occurrences,
                            "unique_senders": unique_senders,
                            "reason": (
                                f"Same content from {sender_domain} "
                                f"({r['occurrence_count']}×)"
                            ),
                        }

        # Multi-sender campaign: same content from multiple senders
        if unique_senders >= _REPEATED_DIFF_SENDER_MIN:
            return {
                "is_repeated": True,
                "occurrence_count": total_occurrences,
                "unique_senders": unique_senders,
                "reason": (f"Same content from {unique_senders} different senders"),
            }

        return {
            "is_repeated": False,
            "occurrence_count": total_occurrences,
            "unique_senders": unique_senders,
            "reason": "",
        }

    # ── Stats ──────────────────────────────────────────────────────────────

    def count_signatures(self, account_email: str | None = None) -> int:
        """Count stored spam signatures."""
        if account_email:
            row = self._db.execute_one(
                "SELECT COUNT(*) AS cnt FROM spam_signatures WHERE account_email = ?",
                (account_email,),
            )
        else:
            row = self._db.execute_one(
                "SELECT COUNT(*) AS cnt FROM spam_signatures",
            )
        return row["cnt"] if row else 0

    def count_content_hashes(self, account_email: str | None = None) -> int:
        """Count stored content hash entries."""
        if account_email:
            row = self._db.execute_one(
                "SELECT COUNT(*) AS cnt FROM spam_content_hashes "
                "WHERE account_email = ?",
                (account_email,),
            )
        else:
            row = self._db.execute_one(
                "SELECT COUNT(*) AS cnt FROM spam_content_hashes",
            )
        return row["cnt"] if row else 0

    def get_stats(self) -> dict[str, int]:
        """Get aggregate statistics for the similarity detection system.

        Returns:
            Dict with ``signatures`` and ``content_hashes`` counts.
        """
        return {
            "signatures": self.count_signatures(),
            "content_hashes": self.count_content_hashes(),
        }


__all__ = [
    "SpamSimilarityDetector",
    "_content_hash",
    "_extract_domain",
    "_jaccard_from_bytes",
    "_make_shingles",
    "_minhash_signature",
]
