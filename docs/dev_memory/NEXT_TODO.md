# NEXT_TODO — 다음 할 일 목록

_완료 항목은 삭제하거나 SESSION_LOG로 이관_

---

## 레짐(Regime) 실데이터 파이프라인 — 2026-07-10 배선 완료, 재시작 및 후속 확인 필요

- [x] 관측 루프 재시작 — 2026-07-10 중 Cross-asset stress 작업으로 여러 차례 재시작됨(아래 새 절 참고).
- [ ] 큰 갭이 있는 날 `compute_gap_zscore` 기반 TREND_UP/DOWN·VOL_EXPANSION·CRISIS_DEFENSE
      전환이 실제로 동작하는지 확인. 갭이 작은 평상시엔 전일 마감 레짐을 그대로 물려받는 게 정상 동작.
- [ ] `feature_store`에 §7.3 6개 피처가 매분 실제로 쌓이는지 DB로 확인(`symbol="KOSPI200"`,
      `feature_version="v1"`).
- [ ] `feature_store`가 20영업일 이상(대략 8,000행) 쌓이면 `python scripts/fit_regime_engine.py` 실행 →
      `data/models/regime_engine.pkl` 생성 → 다음 재시작부터 `RegimeStateMachine`이 자동으로 predict()
      모드로 전환되는지 확인.
- [ ] `cross_asset_stress`(USDKRW·USDCNH·US10Y)는 여전히 0.0 고정 스텁 — **원시 데이터는 2026-07-10
      `macro_snapshot_5m`으로 수집되기 시작함**(아래 새 절 참고). 남은 일은 `regime_features.py`의
      `cross_asset_stress()`를 `db.latest_macro_snapshot()` 기반 실계산으로 교체하는 배선뿐([[DECISION_LOG]]
      2026-07-10 "Cross-asset stress는 스팟/지수가 아니라..." 항목이 "How to apply"에 이 부분 언급).
      USDKRW는 아직 아무 경로로도 안 채워짐 — `market_raw_1m.usdkrw` 컬럼도 계속 비어있는 채로 남음,
      별도 확인 필요.
- [ ] `macro_score`는 현재 외국인 순매수 부호 근사치(`compute_macro_score_proxy`) — 이제 `macro_snapshot_5m`에
      VIX 기간구조가 있으니 이걸 포함한 완전한 매크로 나침반(§8)으로 교체 검토.
- [ ] `rv_ratio`(RV5d/RV20d)는 선물 심볼이 분기 롤오버될 때마다 일별 종가 이력이 끊긴다(현재 심볼
      기준으로만 `daily_closes` 조회) — 롤오버 연속성 처리 필요 여부 검토.

## Cross-asset stress 매크로 스냅샷 — 2026-07-10 구현, ZN(US10Y)만 CBOT 계좌 승인 대기

- [ ] **CBOT 신청 처리 여부 재확인** — 사용자가 신청 완료했다고 확인했지만 재시작 후 세 사이클 연속
      `EGW00552: CBOT SUB거래소 신청 상태가 아닙니다`로 계속 거부됨([[SESSION_LOG]]/[[DECISION_LOG]]
      2026-07-10 항목 참고). KIS 앱/HTS에서 신청 상태가 실제로 "승인 완료"인지, 모의투자 계좌 쪽에도
      반영되는 신청인지 확인 필요. 열리면 `macro_snapshot_5m.zn_front`가 재시작 없이 다음 5분
      사이클부터 자동으로 채워진다(코드 변경 불필요).
- [ ] `regime_features.cross_asset_stress()` 배선(위 레짐 절 항목과 동일 — 여기서도 교차 참조).
- [ ] `poll_macro_snapshot`이 정규장 하루 운영 후 실제로 5분 간격을 안정적으로 지키는지, 다른 폴러들과의
      레이트리밋 경합으로 유실되는 사이클이 있는지 DB로 확인(다른 세 폴러는 2026-07-09에 이미 이 문제를
      겪고 고정틱 스케줄링으로 고쳤음 — `poll_macro_snapshot`도 같은 패턴을 이미 쓰고 있지만 실운영
      검증은 아직 안 함).

## 관측 인프라(Phase 1) 마무리

- [x] (2026-07-19 로깅만 완료, 근본원인 확인은 남음) **NumericValueOutOfRange 진단 로깅** — 2026-07-16
      점검에서 특정 10개 행사가(1087.5~1097.5, 1160~1170 두 클러스터)의 IV 등이 DECIMAL(8,6) 범위를
      계속 넘어 레그 삽입이 실패(3,416회) 중임을 발견. `_parse_option_quote()`가 원본 `output1`을
      row에 `_raw_kis_output1`로 함께 실어 나르고, 삽입 실패 로그에 그대로 찍히도록 수정함([[SESSION_LOG]]
      2026-07-19 항목 참고).
  - [ ] 다음 재발 시 로그의 `raw_kis_output1`을 실제로 보고 어떤 필드(delta_val/gama/theta/vega/
        hts_ints_vltl/hist_vltl 등)가 어떤 비정상 값(음수/특수 sentinel/자릿수 오류 등)을 반환하는지
        확인해 근본 수정.
  - [ ] 두 클러스터(약 70pt 간격)가 정말 서로 다른 북(먼슬리/위클리월/위클리목)의 ATM 근방인지
        DB(`option_analysis_1m`은 실패라 안 남으므로 subscription_manager 로그/각 북의 desired_strikes
        스냅샷 등으로)로 교차 확인.
- [x] `nearest_expiry_chain()`으로 얻은 심볼 목록을 순회하며 `rest_client.get_quote()` 반복 호출 →
      `option_analysis_1m`(IV/Greeks/OI/GEX 등) 적재 루프를 `main.py`에 연결 — 2026-07-06 `poll_option_chain()`으로
      구현 완료([[SESSION_LOG]] 참고). 단, 범위는 `nearest_expiry_chain()`(체인 전체)이 아니라 WS와 동일한
      ATM±3(`subscription_manager.desired_strikes`)로 한정 — 레이트리밋/지연 우려로 의도적으로 좁힘.
- [x] `main.py`가 옵션 체인(선물 1건이 아니라 여러 옵션 심볼)에 대해 WS 구독을 실제로 검증 —
      2026-07-06 실데이터로 검증 중 심볼 혼입 버그 + WS 헤더 파싱 버그 둘 다 발견·수정함([[SESSION_LOG]] 참고)
- [ ] 심볼별 aggregator 분리 수정 이후, 정규장 중 각 종목(콜/풋 개별)의 봉이 실제로 합리적인 OHLC 범위를
      유지하는지(더 이상 다른 종목과 안 섞이는지) 추가 관찰 필요 — 오늘은 수정 직후라 확인 시간이 짧았음
- [x] (2026-07-19 해소) **WS 재연결 로직** — `run_observation_loop_forever()`(신규, `mahdi/main.py`)가
      `run_observation_loop()`을 감싸 WS 단절(OSError/websockets.WebSocketException) 시 죽지 않고
      지수 백오프(5초→최대 60초, 연결 성공 시 리셋)로 재연결한다. 재연결마다
      `RollingSubscriptionManager.rebind()`(신규)로 새 클라이언트로 교체 + `desired_strikes`를
      리셋해, 서버 쪽 구독이 재연결로 사라진 것과 무관하게 ATM±N 전체를 새 연결에 처음부터
      다시 구독한다([[SESSION_LOG]] 2026-07-19 항목 참고).
  - [ ] **실운영 확인 필요**: approval_key를 재연결마다 재발급하지 않고 재사용하도록 구현함(REST
        접속키 발급 엔드포인트에 불필요한 부하를 주지 않기 위한 판단) — 실제 장시간 운영 중
        재연결이 발생했을 때 KIS가 오래된 approval_key로도 재연결을 계속 승인하는지 확인 안 됨.
        거부되면(예: 인증 오류로 즉시 재끊김 반복) `ApprovalKeyIssuer(kis_settings).issue()`를
        재연결 경로에도 추가해야 함.
  - [ ] 실제 네트워크 단절(예: Wi-Fi 재시작, KIS 서버 점검)로 재연결이 실전에서 발동하는지,
        재연결 후 몇 분 내로 정상 관측이 재개되는지 로그로 확인 필요(지금까지는 단위테스트로만 검증됨).
- [x] (2026-07-19 문서화만 완료, 스키마/데이터는 그대로) **타임스탬프 정책 명문화** — 2026-07-16
      점검(§3-4)에서 발견: `datetime.now()`(naive, 서버 로컬=KST)를 TIMESTAMPTZ 컬럼에 그대로 써서
      실제로는 KST 벽시계 시각인데 "+00"(UTC)으로 잘못 라벨링된 값이 저장되고 있었음. 사용자에게
      세 가지 방향(문서화만 / TIMESTAMP로 컬럼 타입 변경 / tz-aware 전환+과거데이터 마이그레이션)을
      제시했고 **"문서화만" 선택**(하이퍼테이블 스키마·기존 데이터는 건드리지 않음, 2026-07-19).
      `mahdi/data/db.py`에 `local_now()`(datetime.now()와 동일 동작) 신설 + 큰 docstring으로 이
      규약을 설명, `mahdi/main.py`/`mahdi/engines/regime_pipeline.py`/`mahdi/dashboard/data_source.py`의
      DB 관련 `datetime.now()` 호출을 전부 `db.local_now()`로 교체(동작 변경 없음, 단일 소스화만).
      `db/migrations/008_timestamp_policy_docs.sql`(신규)이 관련 TIMESTAMPTZ 컬럼 15개에
      `COMMENT ON COLUMN`으로 같은 설명을 남김([[SESSION_LOG]] 2026-07-19 항목 참고).
  - [x] (2026-07-19 적용 완료) 마이그레이션을 실행 중인 컨테이너에 직접 적용
        (`docker exec -i mahdi_timescaledb psql -U mahdi -d mahdi < db/migrations/008_timestamp_policy_docs.sql`
        — Docker Desktop이 꺼져있어 먼저 기동, `restart: unless-stopped`로 컨테이너 자동 기동 후
        적용). `pg_catalog.pg_description` 조회로 15개 컬럼 전부 코멘트 반영 확인, 데이터/스키마
        타입은 무변경.
  - [ ] "TIMESTAMP로 컬럼 타입 변경"·"tz-aware 전환+과거데이터 보정" 두 대안은 이번에 보류만
        됐을 뿐 기각된 게 아님 — Phase2 착수 전이나 해외선물 교차분석이 실제로 필요해지는 시점에
        다시 검토할 것(비용: 전자는 TimescaleDB 하이퍼테이블 7개의 파티션 컬럼 타입 ALTER, 후자는
        과거 데이터 9시간 일괄 보정 + 전환 시점 불연속 처리).
- [x] (2026-07-19 구현+라이브 DB 적용 완료) **Slack 능동 알림** — WS 연결 끊김/재연결, option_analysis_1m
      5분 이상 결손+복구, CBOT(zn_front) 미승인 3가지를 Slack으로 알린다(운영점검보고서 §5-4).
      `C:\Users\82108\PycharmProjects\futures`(미륵이)의 utils/notify.py+slack_queue.py 큐+워커 패턴을
      asyncio로 이식(`mahdi/notify.py`, 신규). COCKPIT에 🔔 체크박스(대시보드 상단) + `mahdi/config/settings.py`
      `SlackSettings`(.env: SLACK_BOT_TOKEN/SLACK_CHANNEL_ID/SLACK_ALERTS_ENABLED_DEFAULT) 추가.
      On/Off는 COCKPIT(Streamlit)과 mahdi.main(관측 루프)이 별도 프로세스라 메모리 공유가 안 돼
      `slack_alert_settings` 싱글턴 테이블(DB, `db/migrations/009_slack_alert_settings.sql`)로 공유—
      라이브 컨테이너에 이미 적용 완료(빈 테이블, 아직 아무도 안 건드려 SlackSettings 기본값 True로
      동작 중)([[SESSION_LOG]] 2026-07-19 항목 참고).
  - [ ] **실제 Slack 메시지 발송 검증 안 됨** — 이번 세션에선 단위테스트(가짜 httpx.AsyncClient)로만
        검증했고, 실제 봇 토큰으로 진짜 채널(C0BJ7R4MZ9B)에 메시지가 도착하는지는 확인 안 함. 관측
        루프 재시작 후(또는 수동 스모크 테스트로) 첫 알림이 실제로 오는지 확인 필요.
  - [ ] COCKPIT 체크박스를 브라우저에서 실제로 토글해 DB(`slack_alert_settings.enabled`)에 반영되고,
        관측 루프가 재시작 없이 그 값을 바로 따르는지 실운영 확인 필요(단위테스트로는 로직만 검증됨).
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
- [x] `find_gamma_flip`(options_intel.py)이 COCKPIT 리런마다 vollib에서 `RuntimeWarning: divide by
      zero`/`invalid value encountered` 출력 — 2026-07-09 원인 규명(vollib.ref_python.d1()의 디버그용
      `print('')`, iv/t_years=0 경계) 및 `redirect_stdout`+`catch_warnings`로 수정 완료([[SESSION_LOG]],
      [[DECISION_LOG]] 참고). 이게 COCKPIT 하루 로그 99.9%를 차지하던 빈 줄의 정체였음.
- [ ] Flow Radar x축 `rangebreaks`(전일 장마감~당일 개장 공백 제거) — 2026-07-07 코드 수정 완료,
      테스트 통과([[SESSION_LOG]] 참고). **COCKPIT 재시작 후 브라우저에서 실제로 공백이 압축돼
      보이는지 육안 확인 아직 안 함**.

## 운영 검증

- [x] 2026-07-06(월) 07:30 자동 기동 스케줄 실제 동작 확인 (Mahdi-PreMarket-Startup) — 실행은 됐으나 Docker Desktop 미기동으로 DB/Redis 없이 COCKPIT/관측루프만 뜸(수동으로 Docker 기동해 당일 대응, 배치파일에 자동 기동/대기 로직 추가함)
- [x] 2026-07-07(화) 07:30 기동 시 새로 추가한 Docker 자동 기동/폴링 로직이 실제로 동작하는지 확인 — **실패**: schtasks 반환코드 255로 즉시 종료(PC가 07:25:42에 막 부팅돼 Docker 꺼진 채 트리거되며, `start_mahdi_premarket.bat`의 IF 블록 내 미이스케이프 괄호로 인한 cmd.exe 파싱 버그가 처음 실제로 실행되어 노출됨). `^(...^)`로 수정 후 수동 재실행해 당일분 기동 완료([[SESSION_LOG]]/[[DECISION_LOG]] 참고). 2026-07-08 07:30 자동 기동은 정상 동작 확인(2026-07-09 로그 점검, 크래시성 재시작 없이 하루 종일 안정).
- [x] 2026-07-08(수) 15:45 자동 종료 확인 (Mahdi-MarketClose-Shutdown) — 정상 동작 확인(2026-07-09 로그 점검, COCKPIT/관측루프 프로세스 트리 에러 없이 종료, DB/Redis는 유지).
- [x] 정규장 시간 중 `market_raw_1m`/`regime_state`에 실제 1분봉이 쌓이는지 확인 — 2026-07-09 DB 쿼리로 확인. 단, `option_analysis_1m`은 정규장 405분 중 203분(50%)치가 REST 500 대량 유실로 통째로 비어 있었음([[SESSION_LOG]], [[DECISION_LOG]] 참고) — 원인으로 추정되는 레이트리밋 미대응을 2026-07-09 레이트리미터+재시도로 수정. **다음 확인 필요**: 정규장 하루 운영 후 이 공백 비율이 실제로 줄었는지 재확인.
- [ ] **2026-07-09 2차**: 위 대량 유실 수정 후에도 남아있던 5분 간격 잔여 유실(09:03/09:08/09:13/09:18 패턴, 405분 중 4분)을 사용자가 Gamma Wall에서 발견 — `poll_option_chain`/`poll_expiry_liquidity`/`poll_investor_flow`를 절대시각 고정 틱 스케줄링으로 전환 + `poll_expiry_liquidity` 시작 오프셋(30초) 추가로 수정 완료([[SESSION_LOG]], [[DECISION_LOG]] 참고). **다음 확인 필요**: 정규장 하루 운영 후 이 5분 간격 패턴이 실제로 사라졌는지 DB로 재확인.
- [ ] 정규장 시간 중 `logs/observation_loop.log`에 에러 없이 insert가 찍히는지 확인
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
      (실측 상품종류가 공식 문서(L/M)와 다르고 N/O임을 확인, `symbol_master.py` 주석 참고).
      **정정(2026-07-10)**: "L/M은 없다"는 결론이 틀렸음— L/M도 실제 위클리 코드풀로 존재해
      N/O와 함께 조회하도록 확장함(바로 아래 항목, [[DECISION_LOG]] 참고).
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
- [x] Flow Radar "가장 활발한 옵션" 종목이 리런마다 다른 종목으로 튀던 문제 — 2026-07-06 완료
      ([[DECISION_LOG]] 참고). 룩백윈도 누적거래량+`symbol ASC` 타이브레이커로 교체, COCKPIT
      재시작 반영함. **브라우저에서 실제로 안정화됐는지 육안 확인 아직 안 함** — 다음에 COCKPIT을
      열면 Flow Radar 옵션 종목 라벨이 몇 분간 유지되는지 확인할 것.
- [ ] Phase 2: 장전 선발 스코어러 + 일중 강등 트리거 + 북별 파라미터 세트 + 섀도우 30일 검증
      (핵심 규칙: 만기 북은 하루 고정·행사가만 ATM 롤링, 0DTE 방향성 네이키드 매수 금지 — 근거는 연구 문서 참고)
- [x] (2026-07-10 해소) **위클리(목) 확인**: 2026-07-06 당시 상장 전이라 미확인이었던 "위클리(목)"
      체인 — 2026-07-10 마스터파일 재확인 결과 상품종류 `L`(콜)/`M`(풋)에 "위클리C/P" 행이 실제로
      존재함을 확인. 처음엔 "N/O와 L/M이 같은 위클리 상품의 교대 코드풀"로 보고 병합 조회했으나,
      COCKPIT 만기유동성비교 패널이 실제로 표시한 위클리 만기(2026-07-13=월요일)가 그때 더 가까웠던
      N/O 풀에서 나온 것으로 확인돼 **N/O=위클리(월), L/M=위클리(목)인 별개 상품**으로 정정, 사용자
      요청대로 `symbol_master.py`의 series를 `"weekly_mon"`(N/O)/`"weekly_thu"`(L/M)로 분리하고
      `main.py`도 두 북으로 나눠 COCKPIT/Flow Radar에서 월/목이 각각 표시되도록 완료
      ([[DECISION_LOG]]/[[SESSION_LOG]] 2026-07-10 항목 참고).
  - [x] (2026-07-10 교차검증 완료) N/O=월·L/M=목 매핑 — 위클리 분리 반영 재시작 후 실제
        `poll_expiry_liquidity()`가 `weekly_thu`(L/M) 행을 적재했고, 만기가 2026-07-16(목요일,
        달력으로 직접 확인)·잔존일수 6(정확)으로 나와 REST `get_quote()` 기반 실데이터로도
        N/O=위클리(월)/L/M=위클리(목)이 확정됐다.
  - [ ] 위클리를 3번째 북으로 분리하며 `STRIKES_EACH_SIDE`를 3→2로 낮췄다(세 북 모두 ATM±2,
        31/41 슬롯) — 재시작 후 실운영에서 ATM±2로도 관측 품질(감마/유동성 판단)이 충분한지,
        아니면 먼슬리만 ±3으로 복원하는 차등 배분이 필요한지 확인.
- [x] (2026-07-10 원인 수정 완료, 결과 확인은 남음) **위클리(목) 누적거래량이 계속 0**: 사용자가
      COCKPIT에서 발견 — `_parse_asking_price_leg`가 호가(ask/bid)가 없으면 `acml_vol`이 정상
      파싱됐어도 레그 전체를 버리던 결합 버그를 찾아 스프레드/거래량을 독립 파싱하도록 수정
      ([[DECISION_LOG]]/[[SESSION_LOG]] 참고), 관측 루프 재시작 완료.
  - [ ] 재시작 후 다음 몇 사이클에서 위클리(목) 누적거래량이 실제로 0이 아닌 값을 보이는지
        확인 — 계속 0이면 그건 버그가 아니라 "오늘 그 행사가들에 정말 체결이 없다"는 뜻.
- [x] (2026-07-10 해소) **화석 `series='weekly'` 행이 COCKPIT에 영구 노출**: 위클리 분리 반영
      재시작 직후 사용자가 COCKPIT에서 "먼슬리/weekly/위클리(월)" 3행을 보고 정체 확인 요청 —
      `series='weekly'`는 분리 전 구코드가 남긴 화석 데이터(재시작 전 마지막 폴링에서 멈춤)였다.
      `db.latest_expiry_liquidity()`에 `_VALID_EXPIRY_LIQUIDITY_SERIES` 화이트리스트 필터를 추가해
      차단([[DECISION_LOG]] 참고).
  - [x] (2026-07-10 완료) DB에 남아있던 옛 `series='weekly'` 179건을 사용자 확인 후
        `DELETE FROM expiry_liquidity_1m WHERE series='weekly'`로 완전 삭제 —
        `expiry_liquidity_1m`엔 이제 regular/weekly_mon/weekly_thu 세 series만 존재.
