"""Sampler tests: schema validation, blank labels, no-replacement sampling."""

from __future__ import annotations

from src.pipeline.sampler import CsvFormatError, load_tweets, sample_rows


def write_csv(tmp_path, header, rows):
    path = tmp_path / "tweets.csv"
    lines = [",".join(header)] + rows
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_loads_rows_with_blank_suicide_as_none(tmp_path) -> None:
    path = write_csv(tmp_path, ["Tweet", "Suicide"], ['"hello world",', '"gone too soon","Potential Suicide post"'])
    rows = load_tweets(path)
    assert rows[0]["label"] is None
    assert rows[1]["label"] == "Potential Suicide post"
    assert [r["tweet_id"] for r in rows] == [1, 2]


def test_missing_tweet_column_rejected(tmp_path) -> None:
    path = write_csv(tmp_path, ["Text", "Suicide"], ["a,"])
    try:
        load_tweets(path)
    except CsvFormatError:
        return
    raise AssertionError("expected CsvFormatError")


def test_missing_file_rejected(tmp_path) -> None:
    try:
        load_tweets(tmp_path / "nope.csv")
    except CsvFormatError:
        return
    raise AssertionError("expected CsvFormatError")


def test_sample_never_duplicates_and_clamps_to_rows() -> None:
    rows = [{"tweet_id": i} for i in range(1, 12)]  # 11-tweet sample file
    drawn = sample_rows(rows, size=120)
    ids = [r["tweet_id"] for r in drawn]
    assert len(drawn) == 11
    assert len(set(ids)) == 11  # without replacement


def test_sample_with_seed_is_reproducible() -> None:
    import random

    rows = [{"tweet_id": i} for i in range(1, 200)]
    first = sample_rows(rows, size=50, rng=random.Random(42))
    second = sample_rows(rows, size=50, rng=random.Random(42))
    assert [r["tweet_id"] for r in first] == [r["tweet_id"] for r in second]


def test_confusion_counts_match_hand_computed_case() -> None:
    from src.pipeline.validation import confusion

    labeled = [
        {"tweet_id": 1, "label": "Potential Suicide post"},
        {"tweet_id": 2, "label": "Not Suicide post"},
        {"tweet_id": 3, "label": "Potential Suicide post"},
        {"tweet_id": 4, "label": None},
    ]
    results = [
        {"tweet_id": 1, "suicide_likelihood": "high risk", "alert_of_risk": 1},
        {"tweet_id": 2, "suicide_likelihood": "unlikely", "alert_of_risk": 0},
        {"tweet_id": 3, "suicide_likelihood": "neutral", "alert_of_risk": 0},  # false negative
        {"tweet_id": 4, "suicide_likelihood": "potentially likely", "alert_of_risk": 1},  # unlabeled: excluded
    ]
    metrics = confusion(results, labeled)
    assert metrics == {
        "total_labeled": 3,
        "true_positive": 1,
        "false_positive": 0,
        "true_negative": 1,
        "false_negative": 1,
        "accuracy": 0.6667,
        "precision": 1.0,
        "recall": 0.5,
    }