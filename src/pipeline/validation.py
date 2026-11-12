"""Offline-only comparison against the ``[Suicide]`` label.

This module is used after a run to sanity-check the classifier, never during
inference (the label is not sent to the model). The dataset has no confirmed
high-risk class, so the comparison is approximate by design: alert=1 is
contrasted with ``Suicide == "Potential Suicide post"``.
"""

from __future__ import annotations

from typing import Any


def confusion(results: list[dict], labeled_rows: list[dict]) -> dict[str, Any]:
    """Compare predicted alerts against ground-truth labels.

    ``results`` are the run's records; ``labeled_rows`` come from the CSV
    (rows whose label is None are skipped). Non-error records only.
    """
    predicted = {
        r["tweet_id"]: (r.get("alert_of_risk") == 1)
        for r in results
        if "suicide_likelihood" in r
    }
    actual = {
        r["tweet_id"]: (r["label"] == "Potential Suicide post")
        for r in labeled_rows
        if r["label"] is not None
    }
    tp = fp = tn = fn = 0
    for tweet_id, positive in actual.items():
        if tweet_id not in predicted:
            continue
        if predicted[tweet_id]:
            if positive:
                tp += 1
            else:
                fp += 1
        elif positive:
            fn += 1
        else:
            tn += 1
    total = tp + fp + tn + fn
    return {
        "total_labeled": total,
        "true_positive": tp,
        "false_positive": fp,
        "true_negative": tn,
        "false_negative": fn,
        "accuracy": round((tp + tn) / total, 4) if total else None,
        "precision": round(tp / (tp + fp), 4) if tp + fp else None,
        "recall": round(tp / (tp + fn), 4) if tp + fn else None,
    }