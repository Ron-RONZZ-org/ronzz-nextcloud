# ruff: noqa: RUF002
"""Bayesian spam detection — tokenizer, classifier, training.

Uses a chi-squared combination of token probabilities (SpamBayes
algorithm) to classify email messages as spam or ham.  Ships with
a pre-baked seed table for immediate ~80% Day-1 accuracy; per-user
training overrides the seed as feedback accumulates.

Architecture:
    - ``SpamClassifier`` — scores a single message (tokenize → chi-squared)
    - ``SpamTrainer`` — updates per-user token counts from user feedback
    - A seed ``spam_tokens.json`` is shipped with the package for cold-start
    - Per-user training data stored in ``config_dir/spam/user_tokens.json``
"""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mailwatch.paths import config_dir

logger = logging.getLogger(__name__)

# ── Seed data ────────────────────────────────────────────────────────────

_SEED_PATH = Path(__file__).resolve().parent / "spam_tokens.json"

_USER_TOKENS_FILE = "spam/user_tokens.json"


def _user_tokens_path() -> Path:
    return config_dir() / _USER_TOKENS_FILE


# Regex patterns for tokenization
_RE_TOKEN = re.compile(r"[A-Za-z0-9]+|[^\w\s]")
_RE_URL = re.compile(r"https?://[^\s<>\"']+", re.I)
_RE_CAPS_WORD = re.compile(r"\b[A-Z]{4,}\b")
_RE_MONEY = re.compile(r"[\$\€\£\¥]\d+(?:[.,]\d+)?|\d+\s*(?:USD|EUR|GBP)")

# Minimum user reports before fully trusting per-user counts
_MIN_USER_COUNTS = 5

# Spam classification threshold
_SPAM_THRESHOLD = 0.9
_HAM_THRESHOLD = 0.15


def _load_seed() -> dict[str, dict[str, float]]:
    """Load the pre-baked seed token table.

    Returns:
        Dict mapping token → ``{"spam": prob, "ham": prob}``.
    """
    if _SEED_PATH.exists():
        try:
            with open(_SEED_PATH) as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Failed to load spam seed tokens: %s", exc)
    return {}


def _load_user_tokens() -> dict[str, dict[str, dict[str, int | str]]]:
    """Load per-user token data from ``user_tokens.json``.

    Returns nested dict: ``{account_email: {token: {spam_count, ham_count, ...}}}``.
    Returns empty dict on any error.
    """
    path = _user_tokens_path()
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return {}
    except Exception as exc:
        logger.warning("Failed to load user tokens from %s: %s", path, exc)
        return {}


def _save_user_tokens(tokens: dict[str, dict[str, dict[str, int | str]]]) -> None:
    """Atomically write per-user token data to ``user_tokens.json``."""
    path = _user_tokens_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(tokens, f, indent=2, sort_keys=True)
        tmp.replace(path)
    except Exception as exc:
        logger.warning("Failed to write user tokens to %s: %s", path, exc)
        raise


# ── Tokenizer ────────────────────────────────────────────────────────────


def _tokenize(text: str) -> list[str]:
    """Tokenize email text into tokens suitable for Bayesian analysis.

    Splits words, punctuation, URLs, ALL-CAPS sequences, and money
    amounts.  Each token is lowercased for matching.

    Args:
        text: The email subject + body text (plaintext).

    Returns:
        List of token strings.
    """
    tokens: list[str] = []

    # Extract and tag URLs
    urls = _RE_URL.findall(text)
    for url in urls:
        tokens.append("http://")  # generic URL indicator
        # Extract domain from URL for domain-level token
        m = re.match(r"https?://([^/]+)", url)
        if m:
            tokens.append(f"dom:{m.group(1).lower()}")
        text = text.replace(url, "", 1)

    # Extract money amounts
    for _m in _RE_MONEY.finditer(text):
        tokens.append("$")

    # Detect ALL-CAPS flag
    if _RE_CAPS_WORD.search(text):
        tokens.append("ALL-CAPS")

    # Split into individual tokens
    for match in _RE_TOKEN.finditer(text):
        tok = match.group(0).lower().strip()
        if len(tok) >= 2:  # skip single chars
            tokens.append(tok)

    return tokens


# ── Chi-squared combination ──────────────────────────────────────────────


def _chi2_score(probabilities: list[float]) -> float:
    """Combine token probabilities using the chi-squared method.

    Implements the SpamBayes chi-squared combination algorithm.
    The result is a probability 0.0–1.0.

    Args:
        probabilities: List of token spam probabilities (0.0–1.0).

    Returns:
        Combined spam probability.
    """
    if not probabilities:
        return 0.5

    # Pick the N most extreme (farthest from 0.5) tokens
    N = min(15, len(probabilities))
    sorted_probs = sorted(probabilities, key=lambda p: abs(p - 0.5), reverse=True)
    selected = sorted_probs[:N]

    # Protect against extreme values causing log(0)
    eps = 1e-200
    H = -2.0 * sum(math.log(max(p, eps)) for p in selected)
    S = -2.0 * sum(math.log(max(1.0 - p, eps)) for p in selected)

    df = 2.0 * N
    try:
        # Use chi-squared CDF approximation (incomplete gamma function)

        # Compute chi-squared CDF via series expansion
        # H = -2*sum(log(p))   — small if tokens are spammy (p near 1)
        # S = -2*sum(log(1-p)) — small if tokens are hammy (p near 0)
        # h_score = P(χ² < H)  — small if spammy (H small)
        # s_score = P(χ² < S)  — small if hammy (S small)
        # Combined = (s_score - h_score + 1) / 2 for correct polarity
        h_score = _gammainc(df / 2.0, H / 2.0)
        s_score = _gammainc(df / 2.0, S / 2.0)
        return (s_score - h_score + 1.0) / 2.0
    except Exception:
        # Fallback: simple average for edge cases
        return 0.5


def _gammainc(a: float, x: float) -> float:
    """Regularized lower incomplete gamma function P(a, x).

    Uses the series expansion: P(a, x) = exp(-x) * x**a / gamma(a) * sum(...)
    Converges quickly for small x; for large x uses continued fraction.

    Args:
        a: Shape parameter (positive).
        x: Upper limit (non-negative).

    Returns:
        Value in [0, 1].
    """
    if x <= 0 or a <= 0:
        return 0.0

    from math import exp, lgamma, log

    if x < a + 1:
        # Series expansion: P(a,x) = exp(-x + a*ln(x) - ln(Γ(a))) * S
        s = t = 1.0 / a
        for k in range(1, 200):
            t *= x / (a + k)
            s += t
            if abs(t) < 1e-15 * abs(s):
                break
        return s * exp(-x + a * log(x) - lgamma(a))
    else:
        # Continued fraction (Lentz's method)
        from math import fabs

        b = x + 1.0 - a
        c = 1.0 / 1e-30
        d = 1.0 / b
        h = d
        for i in range(1, 200):
            an = -i * (i - a)
            b += 2.0
            d = an * d + b
            if fabs(d) < 1e-30:
                d = 1e-30
            c = b + an / c
            if fabs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if fabs(delta - 1.0) < 1e-15:
                break
        return 1.0 - exp(-x + a * log(x) - lgamma(a)) * h


# ── Classifier ───────────────────────────────────────────────────────────


class SpamClassifier:
    """Bayesian spam classifier using chi-squared token combination.

    Combines a pre-baked seed table (from public corpus) with per-user
    token counts for personalized classification.
    """

    def __init__(self, db: Any = None) -> None:
        """Initialize SpamClassifier.

        Args:
            db: Ignored (kept for backward compatibility). Per-user token
                data is now read from ``config_dir/spam/user_tokens.json``.
        """
        self._seed: dict[str, dict[str, float]] = _load_seed()

    def classify(
        self, subject: str, body: str, account_email: str | None = None
    ) -> dict[str, Any]:
        """Classify a single email message.

        Args:
            subject: Email subject line.
            body: Email body text (plaintext).
            account_email: Account email for per-user training lookup.
                If None, uses only the seed table (global baseline).

        Returns:
            Dict with ``is_spam`` (bool), ``score`` (float 0.0–1.0),
            and ``tokens`` (list of contributing tokens with probabilities).
        """
        text = f"{subject} {body}" if subject else (body or "")
        tokens = _tokenize(text)

        # Deduplicate tokens (each token contributes once per message)
        unique_tokens = list(set(tokens))

        # Load per-user tokens once for this batch
        user_tokens = (
            _load_user_tokens().get(account_email, {}) if account_email else {}
        )

        probs: list[float] = []
        contributing: list[dict] = []

        for token in unique_tokens:
            prob = self._token_probability(token, user_tokens.get(token))
            if prob is not None:
                probs.append(prob)
                contributing.append({"token": token, "probability": round(prob, 4)})

        score = _chi2_score(probs) if probs else 0.5

        # Sort contributing tokens by extremity
        contributing.sort(key=lambda t: abs(t["probability"] - 0.5), reverse=True)

        return {
            "is_spam": score >= _SPAM_THRESHOLD,
            "score": round(score, 4),
            "tokens": contributing[:20],  # top 20 for debugging
        }

    def _token_probability(self, token: str, user_entry: dict | None) -> float | None:
        """Compute combined probability for a single token.

        Blends seed and per-user counts: if user has < ``_MIN_USER_COUNTS``
        observations, blend seed + user; otherwise trust user exclusively.

        Args:
            token: The token string.
            user_entry: Per-user data dict (spam_count, ham_count) or None.

        Returns:
            Probability in [0, 1], or None if token is unknown.
        """
        seed = self._seed.get(token)

        if seed and not user_entry:
            return seed["spam"]

        if user_entry and not seed:
            total = user_entry["spam_count"] + user_entry["ham_count"]
            if total >= _MIN_USER_COUNTS:
                # Robinson's correction
                return (user_entry["spam_count"] + 1.0) / (total + 2.0)
            # Too few observations — treat as neutral
            return 0.5

        if seed and user_entry:
            total = user_entry["spam_count"] + user_entry["ham_count"]
            if total >= _MIN_USER_COUNTS:
                return (user_entry["spam_count"] + 1.0) / (total + 2.0)
            else:
                # Blend: weight seed by (min - total) / min, user by total / min
                weight = total / _MIN_USER_COUNTS
                user_prob = (user_entry["spam_count"] + 1.0) / (total + 2.0)
                return weight * user_prob + (1.0 - weight) * seed["spam"]

        return None


# ── Trainer ──────────────────────────────────────────────────────────────


class SpamTrainer:
    """Updates per-user token counts from user feedback.

    When a user marks a message as spam or ham, this extracts tokens
    and updates ``config_dir/spam/user_tokens.json``.
    """

    def __init__(self, db: Any = None) -> None:
        """Initialize SpamTrainer.

        Args:
            db: Ignored (kept for backward compatibility). Per-user token
                data is now stored in ``config_dir/spam/user_tokens.json``.
        """
        pass

    def report(
        self, subject: str, body: str, account_email: str, is_spam: bool
    ) -> None:
        """Train the classifier on a user's feedback.

        Extracts tokens from the message and increments either
        ``spam_count`` or ``ham_count`` for each.

        Args:
            subject: Email subject line.
            body: Email body text (plaintext).
            account_email: The account this feedback applies to.
            is_spam: True if user marked as spam, False for ham.
        """
        text = f"{subject} {body}" if subject else (body or "")
        tokens = set(_tokenize(text))  # deduplicate per message
        now = datetime.now(UTC).isoformat()
        col = "spam_count" if is_spam else "ham_count"

        all_data = _load_user_tokens()
        acct_data = all_data.get(account_email)
        if acct_data is None:
            acct_data = {}
            all_data[account_email] = acct_data

        for token in tokens:
            entry = acct_data.get(token)
            if entry is None:
                entry = {"spam_count": 0, "ham_count": 0, "last_seen_at": ""}
                acct_data[token] = entry
            entry[col] = entry.get(col, 0) + 1
            entry["last_seen_at"] = now

        _save_user_tokens(all_data)

    def log_feedback(
        self,
        message_uuid: str,
        account_email: str,
        feedback: str,
        db: Any = None,
    ) -> None:
        """Record user feedback in the audit log (stored in ``spam_feedback`` DB table).

        Args:
            message_uuid: UUID of the message.
            account_email: Account email.
            feedback: One of ``"spam"``, ``"ham"``, ``"fraud"``.
            db: Database connection.  Required for writing to ``spam_feedback``
                table (which remains in SQLite).
        """
        if db is None:
            logger.warning("No db provided to log_feedback — skipping feedback record.")
            return
        now = datetime.now(UTC).isoformat()
        db.execute(
            "INSERT INTO spam_feedback (uuid, message_uuid, account_email, feedback, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), message_uuid, account_email, feedback, now),
        )

    # ── Statistics ─────────────────────────────────────────────────────

    def get_stats(self) -> dict[str, int | list[dict]]:
        """Get aggregated stats from user tokens.

        Returns:
            Dict with ``total_tokens``, ``total_spam``, ``total_ham``,
            and optionally ``top_spammy`` (list of top tokens by spam ratio).
        """
        data = _load_user_tokens()
        total_tokens = 0
        total_spam = 0
        total_ham = 0
        token_rows: list[dict[str, Any]] = []

        for acct_email, tokens in data.items():
            for token, entry in tokens.items():
                s = entry.get("spam_count", 0)
                h = entry.get("ham_count", 0)
                total_tokens += 1
                total_spam += s
                total_ham += h
                token_rows.append(
                    {
                        "token": token,
                        "spam_count": s,
                        "ham_count": h,
                        "account_email": acct_email,
                    }
                )

        # Sort by spam ratio descending
        token_rows.sort(
            key=lambda r: r["spam_count"] / max(r["spam_count"] + r["ham_count"], 1),
            reverse=True,
        )

        return {
            "total_tokens": total_tokens,
            "total_spam": total_spam,
            "total_ham": total_ham,
            "top_spammy": token_rows[:20],
        }


__all__ = [
    "SpamClassifier",
    "SpamTrainer",
    "_chi2_score",
    "_tokenize",
]
