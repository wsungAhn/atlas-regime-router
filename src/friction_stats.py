"""Statistical utilities for friction-aware backtests."""

from collections.abc import Callable, Sequence

import numpy as np

DEFAULT_SEED = 20260930
DEFAULT_RESAMPLES = 2000


def _bootstrap_statistics(values: Sequence[float], stat: Callable, n_resamples: int, seed: int) -> np.ndarray:
    values_array = np.asarray(values)
    if values_array.size == 0:
        raise ValueError("values must not be empty")
    rng = np.random.default_rng(seed)
    samples = rng.choice(values_array, size=(n_resamples, values_array.size), replace=True)
    if stat is np.mean:
        return np.mean(samples, axis=1)
    return np.asarray([stat(sample) for sample in samples])


def bootstrap_ci(values, stat=np.mean, alpha=0.10, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> tuple[float, float]:
    """Return a two-sided percentile bootstrap confidence interval."""
    statistics = _bootstrap_statistics(values, stat, n_resamples, seed)
    percentiles = np.percentile(statistics, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return float(percentiles[0]), float(percentiles[1])


def bootstrap_se(values, stat=np.mean, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> float:
    """Return the standard error estimated from bootstrap statistics."""
    return float(np.std(_bootstrap_statistics(values, stat, n_resamples, seed), ddof=1))


def bootstrap_p_one_sided(values, n_resamples=DEFAULT_RESAMPLES, seed=DEFAULT_SEED) -> float:
    """Return a conservative one-sided bootstrap p-value for a positive mean."""
    statistics = _bootstrap_statistics(values, np.mean, n_resamples, seed)
    # The +1 correction avoids reporting an overconfident p-value of exactly zero.
    return float((np.count_nonzero(statistics <= 0) + 1) / (n_resamples + 1))


def paired_diff(pnl_f, pnl_c00) -> np.ndarray:
    """Return filtered-minus-control paired P&L differences."""
    if len(pnl_f) != len(pnl_c00):
        raise ValueError("pnl_f and pnl_c00 must have the same length")
    return np.asarray([(0 if filtered is None else filtered) - control for filtered, control in zip(pnl_f, pnl_c00)])


def holm(pvalues, alpha=0.10) -> list[bool]:
    """Apply Holm's step-down multiple-comparison correction."""
    results = [False] * len(pvalues)
    ordered = sorted(enumerate(pvalues), key=lambda item: item[1])
    for rank, (index, pvalue) in enumerate(ordered, start=1):
        if pvalue <= alpha / (len(pvalues) - rank + 1):
            results[index] = True
        else:
            break
    return results
