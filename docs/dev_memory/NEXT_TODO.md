# NEXT_TODO — 다음 할 일 목록

_완료 항목은 삭제하거나 SESSION_LOG로 이관_

---

## 관측 인프라(Phase 1) 마무리

- [x] `nearest_expiry_chain()`으로 얻은 심볼 목록을 순회하며 `rest_client.get_quote()` 반복 호출 →
      `option_analysis_1m`(IV/Greeks/OI/GEX 등) 적재 루프를 `main.py`에 연결 — 2026-07-06 `poll_option_chain()`으로
      구현 완료([[SESSION_LOG]] 참고). 단, 범위는 `nearest_expiry_chain()`(체인 전체)이 아니라 WS와 동일한
      ATM±3(`subscription_manager.desired_strikes`)로 한정 — 레이트리밋/지연 우려로 의도적으로 좁힘.
- [x] `main.py`가 옵션 체인(선물 1건이 아니라 여러 옵션 심볼)에 대해 WS 구독을 실제로 검증 —
      2026-07-06 실데이터로 검증 중 심볼 혼입 버그 + WS 헤더 파싱 버그 둘 다 발견·수정함([[SESSION_LOG]] 참고)
- [ ] 심볼별 aggregator 분리 수정 이후, 정규장 중 각 종목(콜/풋 개별)의 봉이 실제로 합리적인 OHLC 범위를
      유지하는지(더 이상 다른 종목과 안 섞이는지) 추가 관찰 필요 — 오늘은 수정 직후라 확인 시간이 짧았음
- [ ] WS 연결이 끊겼을 때 재연결 로직 없음 — 장시간 유휴/네트워크 단절 시 그대로 죽음
- [ ] `_option_symbol` 그리드(고정 2.5 간격 ATM±N)와 실제 상장 행사가가 어긋나는 구간을
      실거래로 확인(현재는 `option_symbol()`이 None 반환 시 조용히 스킵만 함)
- [ ] `poll_option_chain()` 범위를 `nearest_expiry_chain()`(체인 전체, ATM±3보다 훨씬 많은 종목)로 넓힐지 결정 —
      넓히면 레이트리밋/폴링 사이클 소요시간 검토 필요
- [ ] `skew_25d`/`spread_state`(`option_analysis_1m`)는 아직 NULL — 25델타 스큐 계산(체인 전체 IV 곡선 필요),
      스프레드 상태 분류 설계 필요
- [ ] `rv_5d`는 KIS `hist_vltl`(과거변동성) 근사치를 그대로 씀 — 정확한 5일 realized vol 재계산으로 교체 검토
- [x] 수급(외국인/기관/개인 순매수) — 2026-07-06 "시장별 투자자매매동향(시세)" API로 `poll_investor_flow()`
      구현 완료([[SESSION_LOG]] 참고, `investor_flow_1m`). 재시작 후 실운영 데이터로 값 범위/추세가 그럴듯한지
      확인 필요.
- [ ] 투자자매매동향 응답(`*_ntby_tr_pbmn`)의 정확한 화폐 단위(원/천원)를 공식 문서/실거래 규모 비교로 확인 —
      확인되면 `position_panel.py`의 "순매수대금" 라벨에 단위를 되돌림
- [x] VPIN(`market_raw_1m.vpin`) — 2026-07-06 선물(H0IFCNT0) 실시간체결가 신규 구독 + `VolumeBucketAggregator`로
      구현, 이후 사용자 요청으로 **옵션에도 종목 구분 없이 통일 적용**([[SESSION_LOG]], [[DECISION_LOG]] 참고).
      재시작 후 실운영 데이터로 선물/옵션 각각 VPIN 값이 0.5(중립) 근처에서 벗어나는 유의미한 변동을
      보이는지 확인 필요.
- [ ] `VPIN_BUCKET_SIZE=50`(등거래량 버킷 크기, 선물·옵션 공용)은 미보정 추정치 — 실거래량 패턴을
      며칠 관찰한 뒤 "일평균거래량/50" 같은 근거로 재조정 검토(특히 옵션은 선물보다 훨씬 얇아 같은
      크기가 적절한지 재검토 필요)
- [x] Flow Radar가 선물만 계속 대표 종목으로 뽑는 문제(VPIN 도입의 부작용) — 2026-07-06 선물/옵션
      계열 분리로 해결, 이후 UI 배치(옵션이 위)/x축 통일/옵션 VPIN 차트까지 2차 개편 완료
      ([[SESSION_LOG]], [[DECISION_LOG]] 참고)
- [ ] `find_gamma_flip`(options_intel.py)이 COCKPIT 리런마다 vollib에서 `RuntimeWarning: divide by
      zero`/`invalid value encountered` 출력 — 크래시는 아니지만 원인 미조사(일부 레그의 t_years나
      iv가 0에 가까운 것으로 추정, 2026-07-06 관찰)

## 운영 검증

- [x] 2026-07-06(월) 07:30 자동 기동 스케줄 실제 동작 확인 (Mahdi-PreMarket-Startup) — 실행은 됐으나 Docker Desktop 미기동으로 DB/Redis 없이 COCKPIT/관측루프만 뜸(수동으로 Docker 기동해 당일 대응, 배치파일에 자동 기동/대기 로직 추가함)
- [ ] 2026-07-07(화) 07:30 기동 시 새로 추가한 Docker 자동 기동/폴링 로직이 실제로 동작하는지 확인(Docker Desktop이 꺼진 상태에서 스케줄러가 트리거되는 시나리오로)
- [ ] 같은 날 15:45 자동 종료 확인 (Mahdi-MarketClose-Shutdown)
- [ ] 정규장 시간 중 `market_raw_1m`/`regime_state`에 실제 1분봉이 쌓이는지, `logs/observation_loop.log`에 에러 없이 insert가 찍히는지 확인
- [ ] COCKPIT의 `st.rerun()` 10초 폴링이 브라우저에서 실제로 갱신되는지, 장시간(하루 종일) 방치 시 메모리/연결 누수 없는지 확인
- [x] `mahdi/dashboard/` 하위 모듈 수정 후 COCKPIT을 재시작해야 반영된다는 사실을 실제로 겪고 확인함
      (2026-07-06, [[DECISION_LOG]] 참고) — 앞으로 대시보드 코드를 고치는 세션에서는 체크리스트에 "COCKPIT
      재시작" 항목을 빠뜨리지 않을 것
- [ ] KIS 토큰 발급 레이트리밋(분당 1회 추정) 실운영 중 재현 여부 관찰 — 오늘 테스트 중 반복 호출로 403 재현됨

## Phase 2(판단·실행) — 아직 시작 안 함

- [ ] Signal Fusion + Meta-Labeling (Triple Barrier, Purged CV)
- [ ] Risk Engine (Kelly 사이징, 한도, Circuit Breaker, Kill Switch) — `mahdi/risk/`는 빈 패키지
- [ ] Execution Engine (Passive-first 진입, 6-Layer Exit, Forced Flat) — `mahdi/execution/`은 빈 패키지
- [ ] 하이브리드 3모드(Advisory→Confirm→Auto)
- [ ] 백테스트 엔진 + 검증 스택(WFO·MC·DSR) — `mahdi/backtest/`는 빈 패키지

## 기타

- [ ] KIS 토큰 폐기(`/oauth2/revokeP`) 호출 경로 없음 — 필요 시 `token_daemon.py`에 추가
- [x] (2026-07-06 해소) 위클리옵션(N/O) 조회 — `symbol_master.py`의 `options()`/`nearest_expiry_chain()`/
      `option_symbol()`이 `series="weekly"` 인자를 받도록 확장 완료(만기일의 월/목 여부는 이름만으로 확정
      불가라 파싱 안 함 — `main.py`가 실제 `get_quote()` 응답의 `futs_last_tr_date`로 확정).

## 만기·종목 선발 체계 (2026-07-06 조사 완료 — [[RESEARCH_EXPIRY_SELECTION_v1]] 참고)

- [x] Phase 1.5-①: `symbol_master`에 위클리(N/O) 조회 추가 — 2026-07-06 완료, 테스트 12개 통과
      (실측 상품종류가 공식 문서(L/M)와 다르고 N/O임을 확인, `symbol_master.py` 주석 참고)
- [x] Phase 1.5-②: 관측 북 2개(최근월 먼슬리 + 최근접 위클리) 동시 구독 — 2026-07-06 완료.
      `main.py`의 `run_observation_loop`가 `subscription_managers: list[...]`를, `poll_option_chain`이
      `books: list[tuple[manager, series]]`을 받도록 시그니처 변경. `main()`이 `monthly_manager`/
      `weekly_manager` 두 개를 만들어 같은 `ws_client`에 동시 구독(슬롯 29/41). 테스트 148개 전체 통과.
      **아직 재시작 안 함** — 반영하려면 관측 루프 프로세스 재시작 필요(사용자 확인 후 진행).
- [x] Phase 1.5-③: `expiry_liquidity_1m` 테이블 신설 — 2026-07-06 완료. 사용자가 "정식 스펙대로"
      선택(%스프레드 포함) — `poll_expiry_liquidity()`가 북(regular/weekly)마다 ATM±2×(C,P)에
      `get_asking_price()`로 Cao-Wei %스프레드·깊이·거래량을 집계, 만기는 ATM 1건만 `get_quote()`로
      확인(북당 사이클당 1건). 레이트리밋 우려 때문에 폴링 주기를 옵션체인(60초)의 5배인 300초로
      완화([[DECISION_LOG]] 참고). 마이그레이션 `005_expiry_liquidity.sql` 적용 완료, 테스트 6개 통과.
      **아직 재시작 안 함** — 실운영 데이터로 레이트리밋 회피 여부/값 범위 확인 필요.
- [x] Phase 1.5-④: COCKPIT 만기별 유동성 비교 패널 — 2026-07-06 완료. `expiry_liquidity_panel.py`
      (Plotly Table, 먼슬리/위클리 %스프레드·깊이·거래량·잔존일수 나란히 표시)를 `app.py`의
      "만기 유동성 비교" 섹션에 배치, `data_source.py`에 `expiry_liquidity` 필드/`db.latest_expiry_liquidity()`
      추가. 테스트 4개 통과. **아직 COCKPIT 재시작 안 함**(모듈 캐싱 — [[DECISION_LOG]] 참고).
- [x] **재시작 완료(2026-07-06 14:2x)**: Phase 1.5-①~④ 전체가 실운영에 반영됨. 재시작 과정에서
      기존 mahdi.main/COCKPIT이 각각 2중 프로세스(.venv python.exe가 anaconda python.exe 자식을
      스폰하는 구조 — 이 PC의 venv 특성으로 보이며 정상, 의도치 않은 중복 세션은 아니었음)로 떠
      있던 것을 정리하고 `uv run` 표준 경로로 1개씩 재시작함. 재시작 직후 위클리 도입으로
      NumericValueOutOfRange 크래시가 실제로 발생 → 바로 위 DECISION_LOG 항목대로 수정 후 재재시작,
      정상화 확인함(`market_raw_1m`에 BAFB*/CAFB*(위클리)와 A01609(선물)/B0160*·C0160*(먼슬리)
      전부 최신 봉 적재 확인, `expiry_liquidity_1m`에 regular 1행 적재 확인, weekly 행은 다음
      폴링 사이클(5분) 대기 중).
- [ ] 다음 확인: (1) `expiry_liquidity_1m`에 weekly series 행도 실제로 쌓이는지 (2) 레이트리밋
      (403/500) 발생 빈도가 300초 주기로 완화됐는지 — 여전히 잦으면 주기를 더 늘리거나 ATM±2를
      ±1로 좁히는 것 검토 (3) COCKPIT "만기 유동성 비교" 패널이 브라우저에서 실제로 두 북을
      보여주는지 육안 확인 (4) 오늘 이전에 있었던 mode="lines+markers" Flow Radar 수정도 이번
      COCKPIT 재시작으로 함께 반영됐으니 별도 재시작 불필요.
- [ ] Phase 2: 장전 선발 스코어러 + 일중 강등 트리거 + 북별 파라미터 세트 + 섀도우 30일 검증
      (핵심 규칙: 만기 북은 하루 고정·행사가만 ATM 롤링, 0DTE 방향성 네이키드 매수 금지 — 근거는 연구 문서 참고)
- [ ] **위클리(목) 확인 필요(2026-07-06 HTS 캡처로 발견)**: eFriend "선물/옵션 종목안내장"에서
      "KOSPI200 위클리(월)"과 "KOSPI200 위클리(목)"이 별도 기초자산으로 분리돼 있음을 확인.
      현재 `symbol_master.py`가 구현한 위클리(N/O, "위클리M " 접두어, W1/W2)는 실측 만기가 전부
      월요일(2026-07-06, 2026-07-13)이라 **"위클리(월)"** 쪽만 커버한 것으로 보임. "위클리(목)"
      체인은 이 시점 기준 상장 전(빈 화면) — 사용자 관찰로는 내일(2026-07-07) 상장 가능성.
      **상장 확인되면**: 마스터파일 재다운로드 후 (1) 목요일 위클리가 N/O를 공유하는지 새 상품종류
      코드를 쓰는지 (2) 기초자산명 컬럼이 "KOSPI200"으로 남는지 "KOSPI200 위클리(목)"처럼 달라지는지
      전수 확인 후 반영 — 상장 전 추측 구현 금지(이번 세션 L/M vs N/O 문서-실물 드리프트 전례 참고).
