"""Pure Black-Scholes option pricing utilities."""

from __future__ import annotations

import math


_SQRT_2 = math.sqrt(2.0)
_MIN_STRIKE_RATIO = 0.01
_MAX_STRIKE_RATIO = 10.0


def _validate_common_inputs(S: float, K: float, T: float, sigma: float) -> None:
    if S <= 0:
        raise ValueError("S must be positive")
    if K <= 0:
        raise ValueError("K must be positive")
    if T <= 0:
        raise ValueError("T must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")


def _validate_option_type(option_type: str) -> None:
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def _d1_d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    sqrt_t = math.sqrt(T)
    vol_term = sigma * sqrt_t
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / vol_term
    d2 = d1 - vol_term
    return d1, d2


def black_scholes_price(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """European option price under Black-Scholes."""
    _validate_common_inputs(S, K, T, sigma)
    _validate_option_type(option_type)

    d1, d2 = _d1_d2(S, K, T, r, sigma)
    discount = math.exp(-r * T)

    if option_type == "call":
        return float(S * _normal_cdf(d1) - K * discount * _normal_cdf(d2))
    return float(K * discount * _normal_cdf(-d2) - S * _normal_cdf(-d1))


def black_scholes_delta(
    S: float,
    K: float,
    T: float,
    r: float,
    sigma: float,
    option_type: str,
) -> float:
    """Black-Scholes delta for calls and puts."""
    _validate_common_inputs(S, K, T, sigma)
    _validate_option_type(option_type)

    d1, _ = _d1_d2(S, K, T, r, sigma)
    if option_type == "call":
        return float(_normal_cdf(d1))
    return float(_normal_cdf(d1) - 1.0)


def strike_for_delta(
    S: float,
    T: float,
    r: float,
    sigma: float,
    target_abs_delta: float,
    option_type: str,
    tol: float = 1e-4,
    max_iter: int = 100,
) -> float:
    """Find the strike whose delta matches the requested absolute delta."""
    if S <= 0:
        raise ValueError("S must be positive")
    if T <= 0:
        raise ValueError("T must be positive")
    if sigma <= 0:
        raise ValueError("sigma must be positive")
    if not (0.0 < target_abs_delta < 1.0):
        raise ValueError("target_abs_delta must be between 0 and 1")
    if tol <= 0:
        raise ValueError("tol must be positive")
    if max_iter <= 0:
        raise ValueError("max_iter must be positive")
    _validate_option_type(option_type)

    target_delta = -target_abs_delta if option_type == "put" else target_abs_delta
    strike_low = S * _MIN_STRIKE_RATIO
    strike_high = S * _MAX_STRIKE_RATIO

    delta_low = black_scholes_delta(S, strike_low, T, r, sigma, option_type)
    delta_high = black_scholes_delta(S, strike_high, T, r, sigma, option_type)

    lower_bound = min(delta_low, delta_high)
    upper_bound = max(delta_low, delta_high)
    if not (lower_bound <= target_delta <= upper_bound):
        raise ValueError("target delta is not bracketed by the fixed strike range")

    low = strike_low
    high = strike_high
    for _ in range(max_iter):
        mid = (low + high) / 2.0
        delta_mid = black_scholes_delta(S, mid, T, r, sigma, option_type)
        if abs(delta_mid - target_delta) <= tol:
            return float(mid)
        if delta_mid > target_delta:
            low = mid
        else:
            high = mid
        if abs(high - low) <= tol:
            return float((low + high) / 2.0)

    raise ValueError("strike_for_delta did not converge within max_iter")
