# Friction calibration 2026-10-19

## Summary

- Grade: FAIL
- Probation stop %: n/a
- n_train / n_validation: 61 / 42
- Boundary: 2026-09-21 13:30:12.148224+00:00
- Data period: 2026-08-20T13:45:00 – 2026-10-19T16:15:00
- Reasons:
  - validation:t_d

## Fit

- Selected β: 0.6250
- Extended / model insufficient: False / False
- β (median / pessimistic): 0.6250 / 0.3750
- β·h (median / pessimistic): 0.6250 / 0.5625

| β | loss |
| --- | --- |
| 0.0000 | 48.0301 |
| 0.1250 | 39.0328 |
| 0.2500 | 13.4605 |
| 0.3750 | 4.9062 |
| 0.5000 | 4.6254 |
| 0.6250 | 4.5701 |
| 0.7500 | 10.2292 |
| 0.8750 | 12.6037 |
| 1.0000 | 14.2132 |
| 1.1250 | 18.7541 |
| 1.2500 | 21.6954 |
| 1.3750 | 24.1680 |
| 1.5000 | 33.4480 |

- Train skips: {'no_bars': 0, 'no_entry_bar': 0, 'no_iv': 0, 'unanchorable': 0, 'no_exit': 0}

| stat | live | sim |
| --- | --- | --- |
| t_a | 0.3934 | 0.4590 |
| t_b | -1.3810 | -1.4322 |
| t_c_i | 0.2432 | 0.3333 |
| t_c_ii | 0.2432 | 0.3333 |

| regime | sim t_a | live t_a | n |
| --- | --- | --- | --- |
| range | 0.6667 | 0.5833 | 24 |
| trend_down | 0.2222 | 0.2222 | 9 |
| trend_up | 0.3571 | 0.2857 | 28 |

## Validation

| target | sim | CI | pass |
| --- | --- | --- | --- |
| t_a | 0.3095 | (0.1429, 0.3810) | True |
| t_b | -1.4305 | (-1.7977, -1.2802) | True |
| t_c_i | 0.4483 | (0.3103, 0.6000) | True |
| t_c_ii | 0.3714 | (0.3226, 0.5806) | True |
| t_d (trend_up) | 0.2381 | (0.0000, 0.1905) | False |

- Overall pass: False

## Controls

| control | sim stats | CI | outside | pass |
| --- | --- | --- | --- | --- |
| A | t_a=0.5476; t_b=-1.2699; t_c_i=0.1579; t_c_ii=0.0709 | ((0.14285714285714285, 0.38095238095238093), (0.3103448275862069, 0.6)) | t_a=True; t_c_i=True | True |
| C | t_a=0.6429; t_b=-1.5604; t_c_i=0.5333; t_c_ii=0.0148 | (0.3226, 0.5806) | t_c_ii=True | True |
- C k: 1.2500

## Sensitivity

| target | sim | CI | pass |
| --- | --- | --- | --- |
| t_a | 0.3478 | (0.1522, 0.3696) | True |
| t_c_i | 0.4333 | (0.2778, 0.5588) | True |

- Sensitivity n: 46
- Low-credit t_b sim / live: -1.4305 / -1.4286
- Low-credit excluded n: 4

## Limitations

- validation n 작음/검정력 약함
- 단일 레짐·train/validation 레짐 차이
- limit 체결 가정 s 밴드
- 페이퍼 체결
- clean이 최악 마찰을 과소대표
- 호가 피드 indicative 여부 미검증
- 이 보고서의 달러 손익은 미보정

## Inputs

| key | value |
| --- | --- |
| date | 2026-10-19 |
| n_episodes_total | 119 |
| exclusion_counts | {'entry_with_error': 1, 'open_no_close': 387, 'crypto': 14472, 'leg_mismatch': 1} |
| missing_symbols | [] |
| data_period | 2026-08-20T13:45:00 – 2026-10-19T16:15:00 |
| r | 0.0450 |
| s | 0.5000 |
| seed | 20260930 |
| n_resamples | 2000 |
