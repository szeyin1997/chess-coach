import pytest
from helper_functions import classify_move


def test_played_best_gives_best_label():
    """played_best=True → label 'Best', severity stays 'Good'."""
    result = classify_move(100, 100, played_best=True)
    assert result["label"] == "Best"
    assert result["severity"] == "Good"


def test_played_best_overrides_excellent():
    """Even with near-zero win_loss, played_best wins."""
    result = classify_move(100, 95, played_best=True)
    assert result["label"] == "Best"
    assert result["severity"] == "Good"


def test_small_win_loss_gives_excellent():
    """win_loss < 2.0 (not played_best) → label 'Excellent', severity 'Good'.
    cp 100→90: win_loss ≈ 0.9 percentage points."""
    result = classify_move(100, 90)
    assert result["label"] == "Excellent"
    assert result["severity"] == "Good"


def test_larger_win_loss_gives_good():
    """win_loss >= 2.0 but still within Good severity → label 'Good'.
    cp 100→50: win_loss ≈ 4.6 percentage points."""
    result = classify_move(100, 50)
    assert result["label"] == "Good"
    assert result["severity"] == "Good"


def test_default_played_best_false_is_backward_compatible():
    """Omitting played_best (default False) never produces 'Best' label."""
    result = classify_move(50, 48)
    assert result["label"] != "Best"
    assert result["severity"] == "Good"


def test_positive_labels_not_applied_to_miss():
    """A Good-severity move that also missed an opportunity stays 'Miss', not 'Best'."""
    # Miss fires when best_eval >> eval_after. Use a big best_eval sentinel.
    result = classify_move(100, 90, best_eval_cp=9500, best_is_winning_shot=True, played_best=True)
    # missed_opportunity fires; label should be "Miss" or a composite, NOT "Best"
    assert result["label"] != "Best"
    assert result["missed_opportunity"] is True


def test_inaccuracy_not_affected():
    """Positive label logic only fires on Good severity; Inaccuracy unchanged."""
    # cp 200→100: delta=-100 (cp boundary), win_loss ≈ 8.5 → Inaccuracy severity.
    result = classify_move(200, 100, played_best=False)
    assert result["severity"] == "Inaccuracy"
    assert result["label"] == "Inaccuracy"
