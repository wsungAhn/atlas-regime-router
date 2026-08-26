"""~/dev/signal-alloc(signal_alloc.py, 2026-08-25 작성)의 그대로 복사본 — 원본
수정 없음, vendor/ 관례(credit_spread_simulator.py와 동일 패턴)를 따른다.
용도: docs/design-multi-asset-combined-backtest.md §5 — 슬리브별 리스크예산
정규화기로만 사용(넷팅 엔진 아님)."""
import math


def signal_to_weights(
    signals: dict[str, float],
    confidences: dict[str, float],
    max_gross: float,
) -> dict[str, float]:
    if signals.keys() != confidences.keys():
        raise ValueError("signals and confidences must have identical keys")
    if not math.isfinite(max_gross) or max_gross <= 0:
        raise ValueError("max_gross must be finite and greater than 0")

    for key in signals:
        signal = signals[key]
        confidence = confidences[key]
        if not math.isfinite(signal):
            raise ValueError(f"signal for {key!r} must be finite")
        if not math.isfinite(confidence) or confidence < 0 or confidence > 1:
            raise ValueError(f"confidence for {key!r} must be finite and in [0, 1]")

    raw = {key: signals[key] * confidences[key] for key in signals}
    gross = sum(abs(value) for value in raw.values())
    if gross == 0:
        return {key: 0.0 for key in signals}

    scale = min(1.0, max_gross / gross)
    return {key: value * scale for key, value in raw.items()}
