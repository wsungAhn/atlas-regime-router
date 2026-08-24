# Atlas Options Hackathon Entry

Alpaca AI Trading Agents Hackathon (2026-08-28 ~ 2026-09-04) 제출용.

## 구조
- `src/signals.py` — 순수 신호계산(레짐분류+매크로게이트+order-intent 생성). 브로커 키 불필요, pytest로 완전 검증.
- 실제 주문 제출은 Alpaca MCP 서버(`.mcp.json`)의 `place_option_order` 도구로 — 이 레포는 그 도구가 받을 intent만 만든다.
- `.env.competition`(gitignore) — 대회 전용 페이퍼계좌 키. `.env.competition.example` 참고.

## 실행
```
.venv/bin/python -m pytest tests/ -q   # 순수로직 검증(키 불필요)
```
MCP 연결 후 실제 사이클은 에이전트(Claude/Codex)가 `src/signals.py::decide_for_symbol()` 결과를
받아 `place_option_order` MCP 도구로 제출하는 루프로 운영한다.

## 리스크 게이트
`src/signals.py` 상단 상수 참고 — 정의된 위험(naked 금지)만, 전략묶음당 2~5%, 동일종목 3%,
포트폴리오 6%, 현금유보 10%, 일일/주간 킬스위치, 매크로 stage4 신규진입 억제(regime-signals 재사용).
