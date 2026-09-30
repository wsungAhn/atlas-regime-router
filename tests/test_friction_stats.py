import numpy as np
import pytest

from src.friction_stats import bootstrap_ci, bootstrap_p_one_sided, bootstrap_se, holm, paired_diff


def test_bootstrap_ci():
    assert bootstrap_ci([2.0] * 50) == (2.0, 2.0)
    low, high = bootstrap_ci([1.0] * 30 + [0.0] * 70)
    assert low == pytest.approx(0.2246, abs=0.03)
    assert high == pytest.approx(0.3754, abs=0.03)
    assert bootstrap_ci([1.0] * 30 + [0.0] * 70) == bootstrap_ci([1.0] * 30 + [0.0] * 70)
    assert bootstrap_ci([1.0] * 30 + [0.0] * 70, n_resamples=101, seed=1) != bootstrap_ci([1.0] * 30 + [0.0] * 70, n_resamples=101, seed=2)
    with pytest.raises(ValueError):
        bootstrap_ci([])


def test_bootstrap_se():
    values = [1.0] * 30 + [0.0] * 70
    assert bootstrap_se(values) == pytest.approx(np.sqrt(0.3 * 0.7 / 100), abs=0.01)
    assert bootstrap_se(values) == bootstrap_se(values)


def test_bootstrap_se_uses_sample_standard_deviation():
    values = np.array([0.0, 1.0])
    n_resamples = 20_000
    seed = 123
    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(n_resamples, values.size), replace=True)
    expected = np.std(np.mean(samples, axis=1), ddof=1)

    assert bootstrap_se(values, n_resamples=n_resamples, seed=seed) == pytest.approx(
        expected, rel=1e-12
    )


def test_bootstrap_p_one_sided():
    assert bootstrap_p_one_sided([1.0] * 50, n_resamples=100) == pytest.approx(1 / 101)
    assert bootstrap_p_one_sided([-1.0] * 50, n_resamples=100) == 1.0
    assert bootstrap_p_one_sided([-1.0, 1.0] * 50) == pytest.approx(0.5, abs=0.1)
    assert bootstrap_p_one_sided([0.0] * 50, n_resamples=100) == 1.0


def test_bootstrap_functions_do_not_change_global_rng_state():
    np.random.seed(123)
    before = np.random.get_state()[1].copy()

    bootstrap_ci([0.0, 1.0], n_resamples=10)
    bootstrap_se([0.0, 1.0], n_resamples=10)
    bootstrap_p_one_sided([0.0, 1.0], n_resamples=10)

    assert np.array_equal(np.random.get_state()[1], before)


def test_paired_diff():
    pnl_f = [1.0, None, 3.0, None, 5.0, None, None, 2.0, 4.0, 6.0]
    pnl_c00 = [0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 1.0, 3.0, 5.0]
    differences = paired_diff(pnl_f, pnl_c00)
    assert len(differences) == 10
    assert differences[[1, 3, 5, 6]].tolist() == [-1.0, -3.0, -5.0, -6.0]
    assert np.mean(differences) == pytest.approx(np.sum(differences) / 10)
    with pytest.raises(ValueError):
        paired_diff([1.0], [1.0, 2.0])


def test_holm():
    assert holm([0.01, 0.04, 0.03]) == [True, True, True]
    assert holm([0.03, 0.04, 0.2]) == [True, True, False]
    assert holm([0.05, 0.06, 0.07]) == [False, False, False]
    assert holm([0.2, 0.03, 0.04]) == [False, True, True]
    assert holm([]) == []
    assert holm([0.01, 0.5, 0.011]) == [True, False, True]
