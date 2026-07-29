import numpy as np
import pytest

from mahdi.engines.regime import RegimeLabel
from mahdi.fusion import trainer


def test_build_training_matrix_empty_input_yields_empty_arrays():
    X, y = trainer.build_training_matrix([])
    assert X.shape == (0, len(RegimeLabel) + 1)
    assert y.shape == (0,)


def test_build_training_matrix_one_hot_encodes_regime_and_labels_profitability():
    rows = [
        {"regime_entry": int(RegimeLabel.TREND_UP_STRONG), "confidence_entry": 0.8, "net_pnl": 15.0},
        {"regime_entry": int(RegimeLabel.RANGE_BALANCED), "confidence_entry": 0.4, "net_pnl": -3.0},
    ]
    X, y = trainer.build_training_matrix(rows)

    assert X.shape == (2, len(RegimeLabel) + 1)
    assert X[0][RegimeLabel.TREND_UP_STRONG] == 1.0
    assert X[0][-1] == 0.8
    assert X[1][RegimeLabel.RANGE_BALANCED] == 1.0
    assert list(y) == [1.0, 0.0]


def test_train_and_round_trip_save_load(tmp_path):
    rows = [
        {"regime_entry": int(RegimeLabel.TREND_UP_STRONG), "confidence_entry": 0.9, "net_pnl": 10.0},
        {"regime_entry": int(RegimeLabel.TREND_DOWN_STRONG), "confidence_entry": 0.9, "net_pnl": -10.0},
        {"regime_entry": int(RegimeLabel.TREND_UP_STRONG), "confidence_entry": 0.8, "net_pnl": 8.0},
        {"regime_entry": int(RegimeLabel.TREND_DOWN_STRONG), "confidence_entry": 0.8, "net_pnl": -6.0},
    ]
    X, y = trainer.build_training_matrix(rows)
    model = trainer.train_tabular_classifier(X, y)

    model_path = tmp_path / "signal_fusion_tabular.pkl"
    trainer.save(model, model_path)
    loaded = trainer.load(model_path)

    assert np.array_equal(loaded.predict(X), model.predict(X))


def test_load_missing_model_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        trainer.load(tmp_path / "missing.pkl")


def test_train_tabular_classifier_single_class_raises():
    X, y = trainer.build_training_matrix(
        [{"regime_entry": int(RegimeLabel.TREND_UP_STRONG), "confidence_entry": 0.5, "net_pnl": 1.0}] * 3
    )
    with pytest.raises(ValueError):
        trainer.train_tabular_classifier(X, y)
