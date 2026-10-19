# Codex Handoff — O2k OS-1 kill-switch pure function — 2026-10-19

> **상태(Status):** `대기중 / pending`
>
> **시작 기록(Started by):** author `session=a43dac5d-41e1-436c-bb1d-f9c0b1f8bc9d machine=mac-studio started=2026-10-19T17:00-0700`
>
> **작성자:** Claude (reviewer) → **수행자:** Codex gpt-5.6-luna (effort medium)
> **작업명:** 새 모듈 `src/os1_kill.py` + `tests/test_os1_kill.py`. 설계 정본: `docs/design-options-only-selling-2026-10-19.md` §3-6 (먼저 읽어라).
> **범위(IN):** 위 파일 2개. **OUT:** 다른 파일 수정(`src/mcp_runner.py` 포함 — 배선은 O6), 네트워크, 실제 `registry/OPTIONS_CLOSE_ONLY` 경로 접근, 커밋.

## 0. 규칙
- `src.` 임포트 금지. 미사용 임포트 금지. 테스트는 **리터럴**로 단언, 구현 상수 임포트 금지. 커밋 금지.
- **실제 레포의 `registry/` 경로를 테스트에서 건드리지 말 것**(tmp_path만).
- 테스트 후 이 파일 `## 8`에 **직접** 작성(감점 사유 먼저, 변이별 pytest 실패 원문, 채팅 출력 금지) → 상태 `검토대기`. 변이는 형제 분기마다 각각.
- 환경: 워크트리 `/Users/Mung/dev/atlas-options-hackathon-worktrees/friction-backtest`, 인터프리터 `/Users/Mung/dev/atlas-options-hackathon/.venv/bin/python`, 테스트 `… -m pytest tests/ -q`, 베이스라인 332 passed(O2가 병렬이면 더 많을 수 있음).

## 1. 계약
```
kill_action(E0, equity_mtm, enabled=False) -> 'none' | 'halt_entries' | 'flatten'
restore_close_only(flag_path) -> bool
```
- `dd = (equity_mtm − E0) / E0`. `enabled=False`면 항상 `'none'`. `E0`가 None/NaN/≤0이거나 `equity_mtm`이 None/NaN이면 `ValueError`(enabled와 무관하게 먼저 검증).
- `dd <= −0.10` → `'flatten'`; `dd <= −0.05` → `'halt_entries'`; 그 외 `'none'`. (경계 `<=`; 한 번의 호출에서 −4.9%→−11% 갭은 `'flatten'`.) HWM(고점) 기준이 **아니라** 고정 `E0` 기준.
- `restore_close_only(flag_path)`: `flag_path`(str 또는 Path, 경로 주입)에 빈 파일을 **생성**(부모 디렉터리는 이미 존재한다고 가정, 없으면 예외 그대로). 이미 있으면 내용·mtime을 건드리지 않고 `False`, 새로 만들면 `True`. **플래그를 삭제하는 함수/코드는 이 모듈에 없어야 한다**(`os.remove`/`unlink`/`rmdir` 등 문자열 부재를 테스트에서 소스 텍스트로 단언).
- 모듈 상단 이름 있는 상수(−0.05, −0.10)를 두되 테스트는 리터럴.

## 2. 필수 테스트
1. E0=50000: 49-경계 리터럴 — equity 47505→`none`(−4.99%), 47500→`halt_entries`(−5.00%), 45005→`halt_entries`, 45000→`flatten`(−10.00%).
2. `enabled=False`(기본)에서는 equity가 10,000이어도 `none`; `enabled=True`를 명시해야 동작.
3. E0 불량 입력 `ValueError`: None, 0, −1, NaN; equity None/NaN.
4. `restore_close_only`: tmp_path에 새로 생성→`True`·파일 존재; 이미 존재(내용 "x")→`False`·내용 "x" 유지.
5. **주입 equity 경로 실호출**: E0=50000 → 47500 → 45000을 `kill_action`에 차례로 넣고, 각 단계 결과에 따라(`halt_entries` 이상이면) `restore_close_only(tmp 플래그)`를 호출. 47500 시점에 플래그 파일이 생기고 결과가 `halt_entries`, 45000 시점에 `flatten`임을 단언. 별도 케이스: 한 폴링에서 50000→45000(=−10%)이 아닌 **−4.9%(47550)에서 −11%(44500)로 갭**이면 `flatten`이고 플래그도 생성됨.
6. 소스 텍스트에 `remove`/`unlink`/`rmdir`/`rmtree`가 없음을 단언(플래그 삭제 API 부재).

## 3. 변이 검증 (각각 pytest 실패 원문을 §8에)
(a) `<=`→`<` (두 임계 각각) (b) `enabled` 기본값을 True로 (c) E0 대신 HWM(이전 최대 equity) 사용 (d) 이미 존재하는 플래그를 덮어쓰기 (e) `ValueError` 검증 제거(각 입력 종류 하나씩) (f) flatten과 halt 우선순위 뒤바꿈 (g) 플래그 삭제 코드 추가.

## 6. Rubric (게이트: 모두 ≥ 4)
| 차원 | 1 | 3 | 5 |
|---|---|---|---|
| 정확성 | 규칙과 다름 | 대부분 맞음 | 임계·경계·기본값·플래그 규칙 정확 |
| 테스트 품질 | 항상 통과 | 일부 | 리터럴 단언, 형제 분기 전부, 변이 원문 증명 |
| 범위 준수 | 범위 밖 수정 | 경미 | 파일 2개만, 레포 registry 미접근, 커밋 없음 |
| 보고 정직성 | 요약만 | 일부 원문 | 변이 원문·감점 사유 먼저·모호점 명시 |

## 8. Executor response (Codex writes here)

(비어 있음)

## 9. Reviewer log (Claude writes here)

(비어 있음)
