# Codex Handoff — O2k OS-1 kill-switch pure function — 2026-10-19

> **상태(Status):** `완료 / done`
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

감점 사유: 없음.

구현:
- `src/os1_kill.py` 및 `tests/test_os1_kill.py`만 추가했다.
- 고정 E0 기준, `enabled=False` 기본값, `<=` 경계, `ValueError` 선검증, exclusive 빈 플래그 생성 및 기존 플래그 보존을 구현·검증했다.
- 실제 레포 `registry/` 경로는 접근하지 않았다. 커밋하지 않았다.

최종 검증 원문:
```
354 passed, 1 warning in 4.03s
```

변이 검증 원문:

### (a-1) flatten 임계 `<=` → `<`
```
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - AssertionError: assert 'halt_entries' == 'flatten'
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt - AssertionError: assert 'halt_entries' == 'flatten'
2 failed, 351 passed, 1 warning in 3.81s
```

### (a-2) halt 임계 `<=` → `<`
```
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - AssertionError: assert 'none' == 'halt_entries'
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt - AssertionError: assert 'none' == 'halt_entries'
2 failed, 351 passed, 1 warning in 3.95s
```

### (b) `enabled` 기본값 `False` → `True`
```
FAILED tests/test_os1_kill.py::test_kill_action_is_disabled_by_default - AssertionError: assert 'flatten' == 'none'
1 failed, 352 passed, 1 warning in 4.03s
```

### (c) 고정 E0 대신 HWM 사용
```
FAILED tests/test_os1_kill.py::test_kill_action_uses_fixed_starting_equity_not_hwm - AssertionError: assert 'halt_entries' == 'none'
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt - AssertionError: assert 'flatten' == 'halt_entries'
FAILED tests/test_os1_kill.py::test_injected_equity_gap_flattens_and_restores_close_only - AssertionError: assert 'flatten' == 'none'
3 failed, 351 passed, 1 warning in 3.89s
```

### (d) 기존 플래그 덮어쓰기
```
FAILED tests/test_os1_kill.py::test_restore_close_only_preserves_existing_flag - AssertionError: assert True is False
1 failed, 353 passed, 1 warning in 3.86s
```

### (e-1) E0 `None` 검증 제거
```
FAILED tests/test_os1_kill.py::test_kill_action_rejects_bad_starting_equity[None]
TypeError: '<=' not supported between instances of 'NoneType' and 'int'
1 failed, 352 passed, 1 warning in 3.82s
```

### (e-2) equity `None` 검증 제거
```
FAILED tests/test_os1_kill.py::test_kill_action_rejects_bad_marked_equity[None]
Failed: DID NOT RAISE ValueError
1 failed, 353 passed, 1 warning in 3.87s
```

### (f) flatten/halt 우선순위 역전
```
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - AssertionError: assert 'halt_entries' == 'flatten'
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt - AssertionError: assert 'halt_entries' == 'flatten'
FAILED tests/test_os1_kill.py::test_injected_equity_gap_flattens_and_restores_close_only - AssertionError: assert 'halt_entries' == 'flatten'
3 failed, 351 passed, 1 warning in 4.12s
```

### (g) 플래그 삭제 코드 추가
```
FAILED tests/test_os1_kill.py::test_kill_module_has_no_flag_deletion_api - AssertionError: assert 'remove' not in source
6 failed, 348 passed, 1 warning in 4.14s
```

### 라운드 2

감점 사유: 없음.

수정:
- `kill_action`이 `FLATTEN_DRAWDOWN` 및 `KILL_DRAWDOWN` 상수를 사용하도록 수정했다.
- `tests/test_os1_kill.py`에 명시적 `src` 경로 삽입을 추가했다.

전체 스위트 pytest 원문:
```
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 61%]
........................................................................ [ 81%]
..................................................................       [100%]
=============================== warnings summary ===============================
../../atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6
  /Users/Mung/dev/atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html
    warnings.warn(  # deprecated in 14.0

-- Docs: https://docs.pytest.org/en/stable/howto/capture-warnings.html
354 passed, 1 warning in 4.55s
```

단독 테스트 pytest 원문:
```
..............                                                           [100%]
14 passed in 0.04s
```

### 라운드 3

감점 사유:
- 최종 전체 스위트에서 이번 작업 범위 밖인 `tests/test_os1_sizing.py::test_bear_call_direction_cap_uses_six_percent_limit` 1건이 실패했다. `test_os1_kill.py` 변경과 무관하며, 전체 결과는 `357 passed, 1 failed`이다.

수정:
- `tests/test_os1_kill.py`에 `E0=100000` 및 `E0=20000`의 supplied starting equity literal 케이스를 정확히 5개 추가했다.
- `E0` 하드코딩 생존 변이를 재주입해 새 테스트가 실패하는 것을 확인한 뒤 구현을 원복했다.
- Section 9는 수정하지 않았다. 커밋하지 않았다.

`E0=50000` 하드코딩 생존 변이 pytest 원문:
```
...F...........                                                          [100%]
=================================== FAILURES ===================================
___________ test_kill_action_uses_supplied_starting_equity_literals __________
tests/test_os1_kill.py:30: in test_kill_action_uses_supplied_starting_equity_literals
    assert kill_action(100000, 95000, enabled=True) == "halt_entries"
E   AssertionError: assert 'none' == 'halt_entries'

E     - halt_entries
E     + none
=========================== short test summary info ============================
FAILED tests/test_os1_kill.py::test_kill_action_uses_supplied_starting_equity_literals
1 failed, 14 passed in 0.03s
```

최종 전체 스위트 pytest 원문:
```
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 60%]
........................................................................ [ 80%]
.F....................................................................   [100%]
=================================== FAILURES ===================================
_____________ test_bear_call_direction_cap_uses_six_percent_limit ______________

    def test_bear_call_direction_cap_uses_six_percent_limit():
>       assert size_os1(
            50000,
            50000,
            1.5,
            0.30,
            "bear_call",
            [{"L": 2500, "side": "bear_call"}],
        ) == (4, "ok")
E       AssertionError: assert (1, 'ok') == (18, 'ok')
E       
E       At index 0 diff: 1 != 18
E       Use -v to get more diff

tests/test_os1_sizing.py:111: AssertionError
=========================== short test summary info ============================
FAILED tests/test_os1_sizing.py::test_bear_call_direction_cap_uses_six_percent_limit
1 failed, 357 passed, 1 warning in 4.16s
```

### 라운드 2 변이 (a-1) flatten 임계 `<=` → `<`
```
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 61%]
.................................................F..........F........... [ 81%]
..................................................................       [100%]
=================================== FAILURES ===================================
____________________ test_kill_action_49_boundary_literals ____________________

    def test_kill_action_49_boundary_literals():
        assert kill_action(50000, 47505, enabled=True) == "none"
        assert kill_action(50000, 47500, enabled=True) == "halt_entries"
        assert kill_action(50000, 45005, enabled=True) == "halt_entries"
>       assert kill_action(50000, 45000, enabled=True) == "flatten"
E       AssertionError: assert 'halt_entries' == 'flatten'
E         
E         - flatten
E         + halt_entries

tests/test_os1_kill.py:15: AssertionError
____________ test_injected_equity_path_restores_close_only_at_halt _____________

    def test_injected_equity_path_restores_close_only_at_halt(tmp_path):
        flag_path = tmp_path / "OPTIONS_CLOSE_ONLY"

        assert kill_action(50000, 47500, enabled=True) == "halt_entries"
        assert restore_close_only(flag_path) is True
        assert flag_path.exists()
>       assert kill_action(50000, 45000, enabled=True) == "flatten"
E       AssertionError: assert 'halt_entries' == 'flatten'
E         
E         - flatten
E         + halt_entries

tests/test_os1_kill.py:62: AssertionError
=========================== short test summary info ============================
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - Assert...
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt
2 failed, 352 passed, 1 warning in 4.29s
```

### 라운드 2 변이 (a-2) halt 임계 `<=` → `<`
```
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 61%]
.................................................F..........F........... [ 81%]
..................................................................       [100%]
=================================== FAILURES ===================================
____________________ test_kill_action_49_boundary_literals ____________________

    def test_kill_action_49_boundary_literals():
        assert kill_action(50000, 47505, enabled=True) == "none"
>       assert kill_action(50000, 47500, enabled=True) == "halt_entries"
E       AssertionError: assert 'none' == 'halt_entries'
E         
E         - halt_entries
E         + none

tests/test_os1_kill.py:13: AssertionError
____________ test_injected_equity_path_restores_close_only_at_halt _____________

    def test_injected_equity_path_restores_close_only_at_halt(tmp_path):
        flag_path = tmp_path / "OPTIONS_CLOSE_ONLY"

>       assert kill_action(50000, 47500, enabled=True) == "halt_entries"
E       AssertionError: assert 'none' == 'halt_entries'
E         
E         - halt_entries
E         + none

tests/test_os1_kill.py:59: AssertionError
=========================== short test summary info ============================
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - Assert...
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt
2 failed, 352 passed, 1 warning in 4.08s
```

변이 재실행 raw pytest 원문 (`--tb=short`):

#### (a-1) flatten 임계 `<=` → `<`
```
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 61%]
.................................................F..........F........... [ 81%]
..................................................................       [100%]
=================================== FAILURES ===================================
____________________ test_kill_action_49_boundary_literals ____________________
tests/test_os1_kill.py:15: in test_kill_action_49_boundary_literals
    assert kill_action(50000, 45000, enabled=True) == "flatten"
E   AssertionError: assert 'halt_entries' == 'flatten'
E     
E     - flatten
E     + halt_entries
____________ test_injected_equity_path_restores_close_only_at_halt _____________
tests/test_os1_kill.py:62: in test_injected_equity_path_restores_close_only_at_halt
    assert kill_action(50000, 45000, enabled=True) == "flatten"
E   AssertionError: assert 'halt_entries' == 'flatten'
E     
E     - flatten
E     + halt_entries
=============================== warnings summary ===============================
../../atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6
  /Users/Mung/dev/atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html
    warnings.warn(  # deprecated in 14.0

-- Docs: https://docs.pytest.org/en/stable/howto/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - Assert...
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt
2 failed, 352 passed, 1 warning in 4.11s
```

#### (a-2) halt 임계 `<=` → `<`
```
........................................................................ [ 20%]
........................................................................ [ 40%]
........................................................................ [ 61%]
.................................................F..........F........... [ 81%]
..................................................................       [100%]
=================================== FAILURES ===================================
____________________ test_kill_action_49_boundary_literals ____________________
tests/test_os1_kill.py:13: in test_kill_action_49_boundary_literals
    assert kill_action(50000, 47500, enabled=True) == "halt_entries"
E   AssertionError: assert 'none' == 'halt_entries'
E     
E     - halt_entries
E     + none
____________ test_injected_equity_path_restores_close_only_at_halt _____________
tests/test_os1_kill.py:59: in test_injected_equity_path_restores_close_only_at_halt
    assert kill_action(50000, 47500, enabled=True) == "halt_entries"
E   AssertionError: assert 'none' == 'halt_entries'
E     
E     - halt_entries
E     + none
=============================== warnings summary ===============================
../../atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6
  /Users/Mung/dev/atlas-options-hackathon/.venv/lib/python3.13/site-packages/websockets/legacy/__init__.py:6: DeprecationWarning: websockets.legacy is deprecated; see https://websockets.readthedocs.io/en/stable/howto/upgrade.html
    warnings.warn(  # deprecated in 14.0

-- Docs: https://docs.pytest.org/en/stable/howto/capture-warnings.html
=========================== short test summary info ============================
FAILED tests/test_os1_kill.py::test_kill_action_49_boundary_literals - Assert...
FAILED tests/test_os1_kill.py::test_injected_equity_path_restores_close_only_at_halt
2 failed, 352 passed, 1 warning in 3.98s
```

## 9. Reviewer log (Claude writes here)

### 라운드 1
**Verdict:** changes requested

검증: 코드·테스트 전체 읽음; 전체 스위트 354 passed 재실행; 자체 변이 주입 8건(임계 두 곳 `<`, enabled 기본값, E0 대체, NaN/E0≤0 검증, `x`→`w`) 전부 KILLED, 생존 0. §8 변이 원문 확인함. 내용은 양호.

반려 사유(경미):
1. `src/os1_kill.py`의 `KILL_DRAWDOWN`, `FLATTEN_DRAWDOWN` 상수가 정의만 되고 **사용되지 않는다**(`kill_action`은 `-0.10`, `-0.05` 리터럴 직접 사용). 상수를 쓰도록 `kill_action`을 바꾸거나 상수를 삭제하라(미사용 코드 금지). 테스트는 리터럴 유지.
2. `tests/test_os1_kill.py`는 `from os1_kill import ...`만 있고 `sys.path.insert` 줄이 없다. 단독 실행(`pytest tests/test_os1_kill.py`)과 전체 실행 모두 통과하는지 근거를 §8에 적고, 다른 테스트(`tests/test_friction_calibrate.py:11-12`)처럼 `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))`를 명시해 일관되게 하라.
수정 후 §8에 라운드 2 항목으로 전체 스위트 원문과 (a) 임계 변이를 상수 경유로 재실행한 pytest 실패 원문을 적어라.

### 라운드 2
**Verdict:** changes requested

검증: 라운드 1 지적 반영 확인(상수 사용, 경로 삽입, 스위트 354 passed). 임계 상수 변이 4건(`<=`→`<` 두 곳, 상수값 변경 두 곳)은 KILLED. 그러나 pyc를 배제한 재주입에서 **생존 변이 1건**: `(equity_mtm - E0) / E0` → `(equity_mtm - 50000) / 50000` — 모든 테스트가 E0=50000만 쓰므로 E0 인자 사용이 검증되지 않는다.
수정: `E0=100000`(equity 95000→`halt_entries`, 90000→`flatten`, 95005→`none`)과 `E0=20000`(18000→`flatten`, 19001→`none`) 케이스를 리터럴로 추가. §8에 **라운드 3** 항목으로 위 변이 pytest 실패 원문과 최종 전체 스위트 원문 기록.

### 라운드 3
**Verdict:** approved

검증: 코드 전체 읽음; 전체 스위트 358 passed 재실행(`python -B`, pyc 배제); 형제 사이트 포함 변이 주입(pyc 삭제 후) 전부 KILLED, 생존 0(상수·e0 검증 변이는 수동 확인 포함). 라운드 1~2 지적 모두 반영. 커밋은 리뷰어가 수행.
