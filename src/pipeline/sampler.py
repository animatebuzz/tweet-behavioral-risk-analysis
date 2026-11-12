"""CSV loading, schema validation, and random sampling without replacement.

Any CSV with a ``Tweet`` column is accepted (the sample file qualifies);
a ``Suicide`` column may be blank or absent and is loaded but never used at
inference. Sampling uses ``random.sample`` so no tweet is selected twice,
and the draw size is clamped to ``min(size, len(rows))`` so an 11-tweet
sample file processes all 11 tweets.
"""

from __future__ import annotations

import csv
import io
import random
from pathlib import Path
from typing import Iterable, Optional, TextIO


class CsvFormatError(ValueError):
    """Raised when the input CSV is missing required columns or is empty."""


def parse_csv(stream: TextIO) -> list[dict]:
    """Parse CSV text into rows of {tweet_id, tweet, label}."""
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise CsvFormatError("empty CSV content")
    if "Tweet" not in reader.fieldnames:
        raise CsvFormatError(f"missing required 'Tweet' column; found: {reader.fieldnames}")
    rows = []
    for position, record in enumerate(reader, start=1):
        tweet = (record.get("Tweet") or "").strip()
        label = (record.get("Suicide") or "").strip() or None
        rows.append({"tweet_id": position, "tweet": tweet, "label": label})
    if not rows:
        raise CsvFormatError("CSV contains no data rows")
    return rows


def load_tweets(path) -> list[dict]:
    """Read a local CSV into rows of {tweet_id, tweet, label}.

    ``tweet_id`` is the 1-based row position in the file, giving every tweet
    a stable, traceable key. Blank ``Suicide`` values become ``label=None``.
    """
    csv_path = Path(path)
    if not csv_path.is_file():
        raise CsvFormatError(f"file not found: {csv_path}")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return parse_csv(handle)


def load_tweets_text(text: str) -> list[dict]:
    """Parse CSV content from a string (used by the Sampler Lambda)."""
    return parse_csv(io.StringIO(text))


def sample_rows(
    rows: Iterable[dict],
    size: int,
    rng: Optional[random.Random] = None,
) -> list[dict]:
    """Draw ``size`` rows at random without replacement, clamped to len(rows)."""
    pool = list(rows)
    n = max(1, min(int(size), len(pool)))
    sampler = rng or random
    return [pool[i] for i in sampler.sample(range(len(pool)), n)]