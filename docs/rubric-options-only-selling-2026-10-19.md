# Acceptance Rubric — options-only selling strategy design (fixed before writing, 2026-10-19)

Target doc: `docs/design-options-only-selling-2026-10-19.md` (Tier 2, author Opus 5.5).
Grading order (grader = fresh context, not the author): list deductions first -> per-dimension scores -> gate.

| # | Dimension | 1 | 3 | 5 |
|---|---|---|---|---|
| R1 | Evidence fidelity | cites numbers not in the reports, or hides the calibration FAIL | numbers right, caveats partial | every figure traceable to `reports/friction/{calibration,compare}-2026-10-19.*`; calibration FAIL (T-d), single regime, n, uncalibrated dollars, optimism direction stated where each conclusion is used |
| R2 | Rule completeness | rules vague ("hold winners, cut losers") | entry/exit/sizing stated but gaps (expiry day handling, assignment, concurrency) | entry filter, hold-to-expiry mechanics (expiry-day, early assignment/pin risk, ex-dividend), stop rule A vs C decision with evidence, per-position and total open-risk caps, kill switch, all with numeric values |
| R3 | Risk honesty | claims stop protects; ignores that no-stop = full spread loss | notes defined risk | quantifies worst-case per position and aggregate (e.g. 26 concurrent x up to 20% equity), proposes concurrency/size caps as the actual control, states what is NOT validated |
| R4 | Decision usefulness / safety gate | no path to action | path exists, vague criteria | forward paper probation with numeric success/stop criteria, evaluation point, `OPTIONS_CLOSE_ONLY` stays until a Task Contract; a Task Contract draft (scope, entry point, limits, stop conditions) without enabling any live order path |
| R5 | Implementability | cannot be split into tasks | tasks listed, tests unclear | handoff-sized tasks with inputs/outputs/tests, dependency order, what is out of scope, open questions with recommendations |

Gate: every dimension >= 3, mean >= 4.0. Hard fail (regardless of score): any statement that enables live orders without a Task Contract; any dollar figure presented as calibrated; omission of the calibration FAIL.
