# NEXT_TODO — 다음 할 일 목록

_완료 항목은 삭제하거나 SESSION_LOG로 이관_

---

## 관측 인프라(Phase 1) 마무리

- [ ] `nearest_expiry_chain()`으로 얻은 심볼 목록을 순회하며 `rest_client.get_quote()` 반복 호출 →
      `option_analysis_1m`(IV/Greeks/OI/GEX 등) 적재 루프를 `main.py`에 연결
- [ ] `main.py`가 옵션 체인(선물 1건이 아니라 여러 옵션 심볼)에 대해 WS 구독을 실제로 검증 —
      지금은 스캐폴드만 되어 있고 정규장 시간대 실데이터로 검증한 적 없음
- [ ] WS 연결이 끊겼을 때 재연결 로직 없음 — 장시간 유휴/네트워크 단절 시 그대로 죽음
- [ ] `_option_symbol` 그리드(고정 2.5 간격 ATM±N)와 실제 상장 행사가가 어긋나는 구간을
      실거래로 확인(현재는 `option_symbol()`이 None 반환 시 조용히 스킵만 함)

## 운영 검증

- [x] 2026-07-06(월) 07:30 자동 기동 스케줄 실제 동작 확인 (Mahdi-PreMarket-Startup) — 실행은 됐으나 Docker Desktop 미기동으로 DB/Redis 없이 COCKPIT/관측루프만 뜸(수동으로 Docker 기동해 당일 대응, 배치파일에 자동 기동/대기 로직 추가함)
- [ ] 2026-07-07(화) 07:30 기동 시 새로 추가한 Docker 자동 기동/폴링 로직이 실제로 동작하는지 확인(Docker Desktop이 꺼진 상태에서 스케줄러가 트리거되는 시나리오로)
- [ ] 같은 날 15:45 자동 종료 확인 (Mahdi-MarketClose-Shutdown)
- [ ] 정규장 시간 중 `market_raw_1m`/`regime_state`에 실제 1분봉이 쌓이는지, `logs/observation_loop.log`에 에러 없이 insert가 찍히는지 확인
- [ ] COCKPIT의 `st.rerun()` 10초 폴링이 브라우저에서 실제로 갱신되는지, 장시간(하루 종일) 방치 시 메모리/연결 누수 없는지 확인
- [ ] KIS 토큰 발급 레이트리밋(분당 1회 추정) 실운영 중 재현 여부 관찰 — 오늘 테스트 중 반복 호출로 403 재현됨

## Phase 2(판단·실행) — 아직 시작 안 함

- [ ] Signal Fusion + Meta-Labeling (Triple Barrier, Purged CV)
- [ ] Risk Engine (Kelly 사이징, 한도, Circuit Breaker, Kill Switch) — `mahdi/risk/`는 빈 패키지
- [ ] Execution Engine (Passive-first 진입, 6-Layer Exit, Forced Flat) — `mahdi/execution/`은 빈 패키지
- [ ] 하이브리드 3모드(Advisory→Confirm→Auto)
- [ ] 백테스트 엔진 + 검증 스택(WFO·MC·DSR) — `mahdi/backtest/`는 빈 패키지

## 기타

- [ ] KIS 토큰 폐기(`/oauth2/revokeP`) 호출 경로 없음 — 필요 시 `token_daemon.py`에 추가
- [ ] 선물 미니/위클리옵션(D/E/N/O 상품종류) 지원은 `symbol_master.py`에 메서드는 있지만 main.py에서 안 씀
