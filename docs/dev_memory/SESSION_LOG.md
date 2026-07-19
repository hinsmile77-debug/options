# SESSION_LOG — 대화별 작업 이력

_최신 세션이 위에 오도록 역순 정렬_

---

## [2026-07-19] Slack 능동 알림 도입 — 07-16 운영점검보고서 §5 고도화 아이디어 4번 구현

**트리거:** 운영점검보고서 §5-4 "능동 알림 도입"(원안은 텔레그램 예시) — 사용자가 대신 Slack으로
지정하며 실제 채널 ID(C0BJ7R4MZ9B)와 Bot User OAuth Token을 제공, "미륵이"
(`C:\Users\82108\PycharmProjects\futures`) 프로젝트의 Slack 알림 체계를 참조하라고 요청. 대시보드
On/Off 버튼 + settings 설정 항목 추가도 함께 요청.

**미륵이 체계 조사:** `utils/notify.py`(레벨별 아이콘+타임스탬프+PC라벨 붙여 `_send()`) +
`utils/slack_queue.py`(threading.Queue+데몬 워커 스레드, `chat.postMessage` 호출, 실패 시 10분
쿨다운 경고, 성공 시 1초 슬립으로 레이트리밋 준수) 조합. 대시보드 체크박스(PyQt `QCheckBox`)의
`stateChanged`가 `notify.set_slack_enabled()`(프로세스 내 전역 플래그)를 직접 호출 — 미륵이는
대시보드와 알림 발송이 **같은 프로세스**(PyQt 메인 루프)라 전역 변수 공유가 가능했다.

**마흐디와의 구조적 차이(설계 변경 포인트):** 마흐디는 COCKPIT(Streamlit, 별도 프로세스)과
관측 루프(mahdi.main, 별도 프로세스)가 완전히 분리돼 있어 미륵이처럼 전역 변수로 On/Off를 공유할
수 없다 — 그래서 On/Off 진실 공급원을 DB(`slack_alert_settings` 싱글턴 테이블)로 옮겼다. 또
마흐디는 threading이 아니라 asyncio(main()의 asyncio.gather) 기반이라 threading.Queue+워커
스레드 대신 asyncio.Queue+워커 태스크로 이식했다.

**구현:**
- `mahdi/notify.py`(신규): `notify(message, level)` — .env 토큰/채널 미설정 또는 DB On/Off가
  꺼져 있으면 조용히 무시, 그 외엔 큐에 적재만 하고 즉시 반환(호출자가 API 응답을 기다리지 않음).
  `run_slack_worker()` — 큐를 순차 처리하는 백그라운드 태스크(1초 간격, Slack 레이트리밋 준수),
  전송 실패해도 태스크 자체는 안 죽고 다음 메시지를 계속 처리.
- `mahdi/config/settings.py`: `SlackSettings`(`SLACK_BOT_TOKEN`/`SLACK_CHANNEL_ID`/
  `SLACK_ALERTS_ENABLED_DEFAULT`, `.env`) + `get_slack_settings()`. `.env`/`.env.example`에 항목 추가
  (`.env`엔 사용자가 준 실제 토큰/채널 값을 넣음, `.env.example`엔 빈 플레이스홀더).
- `db/migrations/009_slack_alert_settings.sql`(신규): `slack_alert_settings` 싱글턴 테이블
  (`id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id)` 트릭으로 단일 행만 허용). 시드 데이터 없음 —
  행이 없으면 `SlackSettings.slack_alerts_enabled_default`로 폴백(`mahdi/data/db.py`
  `is_slack_alerts_enabled()`/`set_slack_alerts_enabled()` 신규).
- `mahdi/main.py`: 세 지점에 알림 배선 — ①`run_observation_loop_forever()`(WS 재연결, §5-2)에
  "연결됨→끊김"/"끊김→재연결 성공" 상태 전환 시점에만 알림(재시도마다 매번 X, `currently_connected`
  플래그로 판단). ②`poll_option_chain()`에 `OPTION_CHAIN_GAP_ALERT_SECONDS=300`(5분) 이상
  결손 시 경고 1회 + 복구 시 안내 1회(`last_success_time`/`gap_alerted`로 상태 추적). ③
  `poll_macro_snapshot()`에 적재 성공한 첫 사이클에서 `zn_front is None`이면(CBOT 미승인)
  경고 1회(`cbot_alert_sent` 플래그, 프로세스=거래일당 1회). `main()`의 `asyncio.gather`에
  `.env` 설정된 경우만 `notify.run_slack_worker()` 태스크 추가.
- `mahdi/dashboard/data_source.py`: `get_slack_alerts_enabled()`/`set_slack_alerts_enabled()` —
  DB 연결 실패 시 "켜짐(True)"으로 보수적 폴백(꺼짐으로 잘못 표시해 사용자를 안심시키는 것보다
  안전).
- `mahdi/dashboard/app.py`: 제목 아래 🔔 체크박스 — DB 조회값과 위젯값이 다르면(사용자가 방금
  클릭) 즉시 DB에 저장, 다음 리런(10초)부터 최신값 반영.

**검증:** `tests/test_notify.py`(신규 6개 — 미설정/DB꺼짐/정상 경로 게이팅, DB 예외 삼킴, 워커의
전송+레이트리밋+API오류 복원력), `tests/test_data_db.py`(+3, is/set_slack_alerts_enabled),
`tests/test_dashboard_data_source.py`(+4, get/set + DB실패 폴백), `tests/test_main.py`(+2 신규
— 옵션체인 5분 결손+복구 알림, CBOT 알림 1회성 / 기존 WS 재연결 테스트 1개 확장 — 상태전환마다
알림이 정확히 1번씩만 나가는지). `_collect_option_chain_cycle`을 페이크로 바꿔 REST 세부사항과
분리 검증하는 과정에서, 사이클 종료 지연(next_tick 스케줄링 값이 1,2,3...초로 누적)이 재시도
백오프 상수(5.0)와 우연히 같아져 테스트 종료 조건이 오작동하는 걸 발견 → retry_backoff_seconds를
테스트에서만 999.0 같은 확실히 다른 값으로 override해 해결(실제 코드 버그 아님, 테스트 설계 이슈).
`.venv`(Python 3.12) 기준 전체 pytest 246개 통과. `py_compile`로 신규/수정 파일 전부 문법 검증.
Streamlit `AppTest`로 app.py 렌더링을 시도했으나 스크립트 최하단의 `time.sleep+st.rerun()` 자체
루프 때문에 타임아웃되는 것을 확인 — 이건 이 커밋 이전 원본 app.py에도 이미 있던 사전 조건(같은
타임아웃을 원본 코드로도 재현 확인)이라 이번 변경과 무관함. 대신 타임아웃 직전 로그에서 새
체크박스 관련 DB 예외가 설계대로 조용히 처리(로그만 남기고 True 폴백)됨은 확인함.

**라이브 반영:** 사용자 요청으로 마이그레이션 009를 실행 중인 컨테이너에 직접 적용
(`docker exec -i mahdi_timescaledb psql -U mahdi -d mahdi < db/migrations/009_slack_alert_settings.sql`,
Docker Desktop이 이미 떠 있던 상태). `\d slack_alert_settings`로 스키마, `SELECT count(*)`로 빈
테이블(0행) 확인 — 시드 없음 설계대로 SlackSettings 기본값(True)으로 동작 중.

**다음 확인 필요(코드 아님, 실운영 확인 대상 — [[NEXT_TODO]] 참고):** 실제 Slack 메시지가 채널에
도착하는지(단위테스트는 가짜 HTTP 클라이언트로만 검증), COCKPIT 체크박스를 브라우저에서 실제로
토글했을 때 관측 루프가 재시작 없이 반영하는지.

---

## [2026-07-19] 타임스탬프 정책 명문화 — 07-16 운영점검보고서 §5 고도화 아이디어 3번 구현

**트리거:** 2026-07-16 운영점검보고서(§3-4) — `mahdi/main.py`가 전 구간에서 `datetime.now()`
(naive, 서버 로컬시각=KST)를 그대로 TIMESTAMPTZ 컬럼에 써서, 실제로는 KST 벽시계 시각인데
"+00"(UTC)으로 잘못 라벨링된 값이 저장되고 있음을 발견(14:20 KST 조회 결과가
"...14:20:00+00"으로 나옴 — 진짜 UTC라면 05:20이어야 함). §5-3이 제안한 "타임스탬프 정책
명문화"를 실행하되, 앞의 두 항목(§5-1 로깅, §5-2 WS 재연결)과 달리 이건 라이브 프로덕션
DB(TimescaleDB 하이퍼테이블, 매일 데이터 누적 중)의 스키마/과거 데이터를 건드릴 수도 있는
선택이라 먼저 사용자에게 방향을 확인함.

**결정(사용자 확인):** 세 가지 방향(①문서화만/②TIMESTAMP로 컬럼 타입 변경/③tz-aware 전환+
과거데이터 마이그레이션) 중 **①문서화만**을 선택 — 스키마·기존 데이터 전부 무변경, 코드 동작도
100% 동일(datetime.now() 그대로). ②/③은 TimescaleDB 하이퍼테이블 7개의 파티션 컬럼 타입
ALTER나 누적 데이터 9시간 일괄 보정이 필요해 라이브 시스템에서 검증 없이 지금 실행하기엔
리스크가 크다고 판단해 보류(기각 아님 — 다시 검토할 시점은 [[NEXT_TODO]] 참고).

**구현:**
- `mahdi/data/db.py`에 `local_now()`(신규) — 동작은 `datetime.now()`와 완전히 동일하고, 이
  프로젝트의 naive-KST 타임스탬프 규약·왜 이렇게 됐는지·잠재 위험(해외선물 교차분석 시 9시간
  오차, Postgres 서버 함수(NOW()/CURRENT_DATE)와 섞어 쓸 때 날짜 경계 어긋남)을 설명하는 긴
  docstring을 붙임. 모듈 doc스트링에도 "DB에 쓰이는 모든 시각은 이 함수를 거칠 것"을 명시.
- `mahdi/main.py`(5곳: `upsert_active_futures_symbol` 호출 1곳 + `poll_time` 대입 4곳),
  `mahdi/engines/regime_pipeline.py`(2곳: `compute_gap_zscore`/`latest_prior_close_regime`),
  `mahdi/dashboard/data_source.py`(1곳: DB `expiry`(DATE) 비교용 `today`)의 `datetime.now()`를
  전부 `db.local_now()`로 교체 — 전부 이미 `from mahdi.data import db`를 쓰고 있어 추가 임포트
  불필요. `data_source.py`의 `_synthetic_snapshot()`(DB에 안 쓰이는 순수 합성 더미 시각)는
  의도적으로 제외하고 그 이유를 주석으로 남김.
- `db/migrations/008_timestamp_policy_docs.sql`(신규): 영향받는 TIMESTAMPTZ 컬럼 15개
  (`market_raw_1m`/`option_analysis_1m`/`regime_state`/`feature_store`/`prediction_logs`/
  `signal_decisions`/`execution_logs`/`trade_history`(entry_time·exit_time)/`risk_snapshots`/
  `underlying_spot_1m`/`investor_flow_1m`/`active_futures_symbol`/`expiry_liquidity_1m`/
  `macro_snapshot_5m`)에 `COMMENT ON COLUMN`으로 같은 설명을 남김 — 컬럼 타입/데이터는 전혀
  안 바꾸는 순수 메타데이터 변경이라 락 걱정 없이 장중에도 적용 가능.

**검증:** `tests/test_data_db.py`에 `local_now()`가 naive datetime을 반환하는지 확인하는 테스트
추가. 기존 호출부 교체는 전부 `datetime.now()`와 동일한 값을 반환하는 순수 치환이라(모킹하는
테스트가 없었음을 grep으로 확인) 기존 테스트 동작 변화 없음. `.venv`(Python 3.12) 기준 전체
pytest 231개 통과.

**다음 확인 필요(코드 변경 아님, 사용자 액션 필요 — [[NEXT_TODO]] 참고):** 마이그레이션 파일은
git에 커밋되지만 기존 관례(`docker exec -i mahdi_timescaledb psql -U mahdi -d mahdi < 파일`)대로
실행 중인 컨테이너에 수동 적용해야 실제 DB 컬럼에 코멘트가 반영된다 — 이번 세션에서 직접
실행하지는 않음(라이브 컨테이너 접근 여부 확인 없이 실행하는 걸 피함).

---

## [2026-07-19] WS 재연결 로직 — 07-16 운영점검보고서 §5 고도화 아이디어 2번 구현

**트리거:** 2026-07-16 운영점검보고서(§4 약점, §5-2) — "WS 연결이 끊기면 재연결 로직이 아예 없어
그대로 죽는다"는 지적. Phase1(관측)에서는 "관측 공백" 정도지만, Phase2가 실제 포지션을 잡기
시작하면 "포지션을 인지 못 하는" 훨씬 심각한 리스크가 되므로 Phase2 착수 전에 먼저 처리하자는
제안(§5 항목 2)을 실행.

**구현:**
- `mahdi/data/subscription_manager.py` `RollingSubscriptionManager.rebind(ws_client)`(신규): 새
  WS 클라이언트로 교체 + `_desired_strikes`를 비운다. 이게 없으면(단순히 `_ws`만 새 클라이언트로
  바꾸면) `roll_to_spot()`의 diff 로직(겹치는 행사가는 재전송 안 함, `test_roll_to_spot_is_idempotent_for_unchanged_range`가
  이미 보여주던 동작)이 "서버 쪽엔 구독이 하나도 없는데 매니저는 이미 구독했다고 착각"하는
  상태를 만들어 재연결 후 아무것도 재구독되지 않는 버그가 생긴다.
- `mahdi/main.py` `run_observation_loop_forever()`(신규): `run_observation_loop()`을 감싸 WS 단절
  (`OSError`/`websockets.WebSocketException` — `ConnectionError`는 `OSError`의 서브클래스라 포함됨)
  시 죽지 않고 지수 백오프(`WS_RECONNECT_INITIAL_BACKOFF_SECONDS=5.0` → `WS_RECONNECT_MAX_BACKOFF_SECONDS=60.0`,
  연결에 한 번이라도 성공하면 다음 끊김부터 다시 5초로 리셋)로 재연결한다. 재연결마다 새
  `KISWebSocketClient`를 만들고 모든 `subscription_managers`를 `rebind()`한 뒤
  `run_observation_loop()`을 다시 호출 — ATM±N 옵션 전 종목 + 선물 구독이 새 연결에 처음부터
  다시 나간다. DB 오류·`ValueError`(구독 슬롯 한도 등) 같은 "연결 문제가 아닌" 예외는 재시도 없이
  그대로 전파(재시도로 해결 안 되는 문제이므로 사람이 봐야 함).
- `main()`의 `asyncio.gather` 태스크 목록에서 `run_observation_loop(...)` 호출을
  `run_observation_loop_forever(..., approval_key=approval_key)`로 교체. approval_key는
  재연결마다 재발급하지 않고 최초 발급분을 재사용(REST 접속키 엔드포인트에 불필요한 부하를 주지
  않기 위한 판단 — 실운영 검증 필요, [[NEXT_TODO]] 참고). `poll_option_chain`/`poll_expiry_liquidity`
  등 REST 폴러는 이 함수와 독립된 태스크라 WS 재연결 시도 중에도 계속 관측을 이어간다(이번 수정
  전에는 WS 단절 하나가 `asyncio.run(main())`까지 예외를 전파시켜 REST 폴러까지 전부 함께 죽었다).

**검증:** `tests/test_data_subscription_manager.py`에 `rebind()` 후 재구독 여부 테스트 1개,
`tests/test_main.py`에 `run_observation_loop_forever` 테스트 3개(①재연결+전체 재구독 확인,
②connect() 반복 실패 시 백오프가 상한까지 커지고 연결 성공 후 리셋되는지, ③연결 문제가 아닌
예외는 재시도 없이 그대로 전파하는지) 추가. 가짜 `connect()` 팩토리(`_FakeConnectCall`/
`_SingleUseConnectionCM`)로 실제 소켓 없이 연속 끊김·재연결 시나리오를 결정론적으로 재현.
`.venv`(Python 3.12) 기준 전체 pytest 230개 통과.

**다음 확인 필요(코드 변경 아님, 실운영 확인 대상 — [[NEXT_TODO]] 참고):** approval_key 재사용이
실제 장시간 재연결 시나리오에서도 KIS에 거부되지 않는지, 실제 네트워크 단절로 재연결이 실전에서
정상 발동하는지는 아직 로그로 확인된 적 없음(지금까지는 단위테스트로만 검증됨).

---

## [2026-07-19] NumericValueOutOfRange 진단 로깅 — 07-16 운영점검보고서 §5 고도화 아이디어 1번 구현

**트리거:** 2026-07-16 운영점검보고서(§3-1) — 옵션체인 특정 10개 행사가(1087.5~1097.5, 1160~1170
두 클러스터)에서 `psycopg.errors.NumericValueOutOfRange`(IV 등 DECIMAL(8,6) 범위 초과)로 레그 삽입
실패가 30만 줄 로그 구간에서 3,416회 재현 중. 2026-07-06에 "레그 단위 try/except+rollback"으로
크래시는 막았지만(§4-1), **당시엔 strike/option_type만 로깅해 어떤 raw 값이 왜 범위를 넘었는지
근본 원인은 조사된 적이 없었다**. 보고서 §5-1이 제안한 "매크로 폴러의 `_log_kis_call_failure()` 패턴
(원본 응답을 그대로 로깅)을 옵션체인 폴러에도 이식"을 실행.

**구현:**
- `mahdi/main.py` `_parse_option_quote()`: 파싱 성공 시 row에 `_raw_kis_output1`(파싱 전 KIS
  `output1` 원본 dict)을 함께 실어 반환하도록 변경. `_OPTION_ANALYSIS_1M_COLUMNS`에 없는 키라
  `db._upsert()`가 `row.get(c) for c in columns`로 컬럼만 골라 쓰므로 INSERT 쿼리엔 안 섞임(스키마
  변경 없음, `_log_kis_call_failure`처럼 별도 함수로 안 빼고 row에 얹는 방식 선택 — 삽입 실패 시점엔
  이미 REST 호출이 끝난 뒤라 exc가 `httpx.HTTPStatusError`가 아니고 psycopg 예외이므로 그 함수를
  그대로 재사용할 수는 없었음).
- `poll_option_chain()`의 레그 삽입 실패 로그(기존 `strike=%s type=%s`만 있던 곳)에
  `raw_kis_output1=%s`를 추가해 원본 KIS 필드(delta_val/gama/theta/vega/hts_ints_vltl/hist_vltl 등)가
  그대로 남도록 함.

**검증:** `tests/test_main.py`에 신규 테스트 2개 추가 — `_parse_option_quote`가 row에 원본 output1을
그대로 담는지, DB 삽입 실패 시 caplog로 실제 로그 문자열에 raw 필드(`hts_ints_vltl` 등)가 찍히는지.
`.venv`(Python 3.12) 기준 전체 pytest 226개 통과(3.7 아나콘다 env는 `typing.Protocol` 미지원으로
이 프로젝트에 안 맞음 — 앞으로는 `.venv/Scripts/python.exe`로 실행할 것).

**다음 확인 필요(코드 변경 아님, 실운영 확인 대상 — [[NEXT_TODO]] 참고):** 이 커밋만으론 근본 원인이
바로 안 밝혀진다 — 다음 재발 시 로그에 찍힐 `raw_kis_output1`을 실제로 봐야 원인 필드(예: 음수/특수
sentinel 등)를 특정할 수 있다. 두 클러스터(70pt 간격)가 정말 서로 다른 북(먼슬리/위클리월/위클리목)의
ATM 근방인지도 그때 함께 확인.

---

## [2026-07-10] Cross-asset stress(VIX 기간구조·USDCNH·US10Y) KIS 데이터 소스 조사·구현 — ZN(US10Y)만 CBOT 계좌 신청 대기로 미완

**트리거:** 사용자가 "§7.3 Cross-asset stress 피처(USDKRW·USDCNH·US10Y 급변)를 KIS API로 구성할 수 있게
수집하자, KIS github(stocks_info)로 확보 방안을 조사해"라고 요청. 이 코드베이스엔 USDCNH/US10Y/VIX 수집
경로가 전혀 없었고(`market_raw_1m.usdkrw` 컬럼도 아무도 안 채움), 위 레짐 배선 작업 때도 데이터 소스가
없어 `cross_asset_stress()`를 0.0 스텁으로 남겼던 바로 그 항목([[DECISION_LOG]] 2026-07-10 레짐 항목 참고).

**조사(KIS GitHub `open-trading-api` 실제 클론 + 마스터파일 다운로드로 검증):**
- VIX 현물지수는 KIS에 아예 없음 — 대신 CBOE VIX 선물(품목코드 `VX`)이 해외선물옵션 도메인에 있어
  근월·차근월 스프레드로 기간구조(콘탱고/백워데이션)를 구성(이게 오히려 학계 표준 정의).
- USDCNH도 온셰어 CNY 환율(해외주식 X구분)이 아니라 HKEx 상장 `CNH` 선물로 "USDCNH(HKEX)" 라벨 그대로 존재.
- US10Y는 두 경로 확인: ① 해외주식 국채구분(I) 일봉 API — 계좌 제약 없이 실제 수익률(%, 심볼 `Y0202`)을
  주지만 분봉은 `ERROR INVALID FID_COND_MRKT_DIV_CODE`로 거부돼 일봉이 한계. ② CME/CBOT `ZN`(10년 국채선물)
  — 5분봉 가능하지만 **CBOT은 별도 거래소 신청이 계좌에 걸려 있어야** 함(모의투자 앱키로 VX/CNH는 즉시
  성공, ZN만 `EGW00552: CBOT SUB거래소 신청 계좌가 아닙니다`로 거부됨을 실측 확인).
- `ffcode.mst`(해외선물 마스터)에 최근월물여부 플래그가 있어 `symbol_master.py`와 같은 패턴으로 만기
  롤오버를 자동화할 수 있음을 확인.

**구현:**
- `mahdi/data/overseas_future_master.py`(신규): `ffcode.mst`(고정폭 포맷) 다운로드·파싱, 품목코드별
  근월·차근월 단축코드 조회(`front_two_codes`).
- `mahdi/broker/tr_codes.py`: 해외선물옵션 시세(`HHDFC55010000`)·해외주식 국채일봉(`FHKST03030100`) 상수 추가.
- `mahdi/broker/rest_client.py`: `get_overseas_future_price()`/`get_overseas_daily_chartprice()` 추가.
- `db/migrations/006_macro_snapshot.sql`: `macro_snapshot_5m` 하이퍼테이블(vix_front/vix_next/
  vix_term_structure/usdcnh/us10y_yield/quality_flag) 신규.
- `mahdi/data/db.py`: `insert_macro_snapshot_5m`/`latest_macro_snapshot`(us10y_yield가 하루 대부분 NULL이라
  LOCF 폴백 포함) 추가.
- `mahdi/main.py`: `poll_macro_snapshot()` 5분 폴러 신규(VX 근월·차근월, CNH 근월, US10Y 일봉을 부분 실패
  허용으로 수집) — `main()`의 `asyncio.gather`에 배선, 해외선물 마스터 로드 실패 시 이 폴러만 건너뜀.
- `mahdi/dashboard/panels/macro_panel.py`(신규) + `data_source.py`/`app.py` 연동 — COCKPIT에 "Cross-asset
  Stress" 표 섹션 추가(콘탱고/백워데이션 라벨, US10Y 없을 때 안내 캡션).
- (CBOT 승인 후 추가) `db/migrations/007_macro_snapshot_zn.sql`: `zn_front` 컬럼 추가 — ZN 선물가는
  수익률과 역상관이라 `us10y_yield`(실제 %)와 단위가 달라 별도 컬럼으로 분리. `poll_macro_snapshot`에
  ZN 근월물 조회 추가, `_log_kis_call_failure()` 헬퍼로 실패 시 KIS 에러 바디(rt_cd/msg_cd/msg1)를 함께
  로깅하도록 개선(500만 봐서는 레이트리밋/계좌 미신청/일시 장애를 구분할 수 없었음).

**운영 조치:** 이미 실행 중이던 `mahdi.main`(사용자가 별도로 띄워둔 프로세스, PID로 발견)을 새 코드
반영을 위해 총 세 차례 재시작 — 매번 `docker exec ... psql < 마이그레이션.sql`로 먼저 스키마를 적용한
뒤 재시작. KIS 토큰은 분당 1회 발급 제한이 있어, 재시작 직후 검증용 별도 스크립트를 바로 돌리면 방금
재발급된 프로세스 토큰과 충돌할 수 있음을 인지하고 별도 스크립트 실행은 피하고 재시작 자체의 로그로만
검증(재시작 한 번으로 검증까지 겸함).

**검증:** pytest 218→225개 전부 통과. 실제 KIS 모의투자로 VX(`VXN26`/`VXQ26`)·CNH(`CNHN26`)·US10Y 일봉
전부 200 OK 응답과 `macro_snapshot_5m` 실제 적재 확인(`vix_term_structure=+6.8%` 등 정상 계산). COCKPIT도
Streamlit으로 실제 기동해 새 패널이 에러 없이 렌더링됨을 확인.

**알려진 한계(다음 확인 필요):** ① 사용자가 KIS 앱/HTS에서 "CBOT 거래소 신청을 완료했다"고 했지만,
재시작 후 세 번의 5분 사이클(13:12/13:17/13:21) 모두 여전히 `EGW00552`로 거부됨 — 신청이 KIS 쪽에서
아직 처리되지 않은 것으로 보임(익영업일 배치 처리 가능성, 계좌 종류 불일치 가능성 등). 코드는 이미
완성돼 있어 KIS 쪽에서 실제로 열리면 재시작 없이 다음 사이클부터 자동으로 채워진다. ② 이번 작업은
"원시 매크로 데이터 수집·표시"까지만 — `mahdi/features/regime_features.py`의 `cross_asset_stress()`
스텁(레짐 엔진 입력)을 `macro_snapshot_5m`을 쓰도록 실데이터로 교체하는 배선은 아직 안 함
([[NEXT_TODO]] 참고).

---

## [2026-07-10] 레짐이 하루종일 "평균회귀"/REGIME_UNSTABLE에 고정되는 문제 조사 → 실데이터 파이프라인 배선

**트리거:** 사용자가 COCKPIT 스크린샷(현재 레짐=평균회귀, 레짐 안정성=REGIME_UNSTABLE, 확률 막대가
평균회귀만 100%·나머지 7개 전부 0%)을 두 차례(시간차를 두고 기초자산가만 다름) 보여주며 "레짐
데이터가 정상수집되고 판정되어 올바른 레짐이 표시되고 있는지 점검해"라고 요청.

**조사:** Explore 서브에이전트로 `mahdi/engines/regime.py`/`mahdi/main.py`/`mahdi/dashboard/`를 훑어
근본 원인을 확정: `main.py`의 `run_observation_loop`가 매 분봉마다 `RegimeEngine.predict()`(실제
HMM 분류기)를 전혀 호출하지 않고, `warmup_fallback(RegimeLabel.RANGE_BALANCED, macro_score=0.0,
gap_zscore=0.0)`을 **하드코딩된 인자**로만 반복 호출하고 있었다. `gap_zscore`가 항상 0이라
`warmup_fallback`의 갭 판정 분기(`abs(gap_zscore)>=2.0`)가 절대 참이 될 수 없어 늘 `prior_close_regime`
(하드코딩된 RANGE_BALANCED)을 그대로 반환하고, `stability_flag=False`도 조건 없이 고정 — 스크린샷과
정확히 일치. 재조회 시점(코드 변경 없음, 시세만 이동)에도 동일 증상이 재현돼 스테일 데이터가 아니라
배선 자체가 안 돼 있음을 재확인.

**계획 수립:** `predict()` 완전 전환에는 `fit()`에 "수십 세션 분량" 피처 이력이 필요한데(regime.py
주석) 지금은 축적된 게 전혀 없다는 제약, `cross_asset_stress`(USDKRW/USDCNH/US10Y)·완전한
`macro_score`(VIX 기간구조·S&P선물 등, §8 나침반)는 이 코드베이스에 데이터 소스 자체가 없다는 제약을
사용자와 확인한 뒤, EnterPlanMode로 범위를 명시한 계획을 세우고 승인받음 — 오늘 실데이터로 계산
가능한 부분만 정직하게 구현하고, 안 되는 부분은 중립 스텁+TODO로 남기기로 확정.

**구현:**
- `mahdi/features/regime_features.py`(신규): §7.3 6개 피처(hurst_exponent/adx/rv_ratio/iv_change_rate/
  book_thinning/cross_asset_stress) 순수 함수. book_thinning은 원 스펙(호가 잔량 급감)이 요구하는
  잔량 절대치가 스키마에 없어 `bid_ask_spread` 급확대 z-score로 대리, cross_asset_stress는 데이터
  소스가 없어 0.0 중립 스텁+TODO.
- `mahdi/engines/regime.py`: `RegimeEngine.save()`/`load()` 추가(pickle) — 오프라인 fit 결과를 실시간
  프로세스가 재학습 없이 로드.
- `mahdi/data/db.py`: `get_feature_history`/`latest_regime_before`/`daily_closes` 헬퍼 추가.
- `mahdi/engines/regime_pipeline.py`(신규): `RegimeFeatureBuilder`(선물봉 롤링 윈도), `compute_gap_zscore`
  (전일 마지막 스팟·오늘 첫 스팟·전일 ATM 스트래들 IV로 실계산), `compute_macro_score_proxy`(외국인
  순매수 부호를 매크로 나침반 근사치로 사용, TODO로 완전한 나침반 필요성 명시), `latest_prior_close_regime`
  (하드코딩 대신 DB 조회), `RegimeStateMachine`(모델 파일 있으면 predict(), 없으면 실데이터
  warmup_fallback()).
- `mahdi/main.py`: 하드코딩 호출 제거하고 `RegimeStateMachine.step()`으로 교체. **레짐 갱신을 선물봉
  완성 시에만** 하도록 수정(기존엔 옵션 봉 완성 때도 매번 재계산해 같은 분에 여러 번 덮어쓰던 부수
  버그도 같이 정리). 옵션체인 폴링에서 ATM 근사 IV를 뽑아 상태머신에 흘려주는 배선 추가.
- `scripts/fit_regime_engine.py`(신규): `feature_store` 축적 데이터로 `RegimeEngine.fit()`을 오프라인
  실행해 `data/models/regime_engine.pkl`에 저장하는 배치 — 20영업일 이상 쌓인 뒤 수동 실행.

**검증:** 신규 테스트 32개(`test_features_regime_features.py`/`test_engines_regime.py` save/load
라운드트립/`test_regime_pipeline.py`) 추가, 기존 `test_main.py` 4개는 시그니처 변경(신규
`regime_state_machine` 인자)·"레짐은 선물봉에서만 갱신" 정정에 맞춰 갱신. 전체 199개 통과
(`.venv/Scripts/python.exe -m pytest` — 이 PC 기본 `python`은 conda `py37_32`라 여전히 안 맞음, 매번
확인 필요).

**다음 확인 필요:** (1) 관측 루프 재시작 전까지는 옛 하드코딩 로직이 계속 돈다 — 재시작 필요.
(2) 재시작 후 큰 갭이 있는 날 gap_zscore 기반 TREND/VOL_EXPANSION/CRISIS_DEFENSE 전환이 실제로
동작하는지 확인. (3) `feature_store`가 20영업일 이상 쌓이면 `scripts/fit_regime_engine.py` 실행해
`predict()` 전환이 실제로 되는지 확인. (4) cross_asset_stress/완전한 macro_score는 USDKRW·USDCNH·
US10Y·VIX 기간구조 등 외부 데이터 소스 연동이 필요 — 아직 미착수([[NEXT_TODO]] 참고).

---

## [2026-07-10] 위클리(목) 누적거래량이 계속 0으로 나오는 문제 조사 → 스프레드/거래량 파싱 결합 버그 발견·수정

**트리거:** 화석 `weekly` 행 정리 후 COCKPIT을 다시 본 사용자가 위클리(목) 행의 누적거래량이 0인 걸 보고 "확인해"라고 요청.

**조사:** REST API를 직접 호출해 위클리(목) ATM 부근 종목(B09F6WA87 등)의 실제 응답을 살펴봄 — 도중 KIS 토큰 재발급 제한(짧은 간격 내 재요청 시 403)과 `inquire-asking-price` 자체의 간헐적 500도 함께 관찰됨(레이트리밋/서버 이슈, 별개 사안). `_parse_asking_price_leg()`를 다시 읽어보니 호가(ask/bid)가 하나라도 없으면(`mid<=0` 포함) `acml_vol`이 정상 파싱됐어도 레그 전체를 버리는 구조였음을 확인 — 얇은 신규 위클리 종목은 순간적으로 양쪽 호가가 빈 상태가 흔해, 실제 누적거래량이 있어도 계속 0으로 집계될 수 있는 설계 결함.

**구현:** `_parse_asking_price_leg`가 스프레드/깊이와 거래량을 독립적으로 파싱해 dict로 반환하도록 리팩터링(위 [[DECISION_LOG]] 항목 참고). `poll_expiry_liquidity`의 집계 루프도 각 필드를 개별적으로 `None` 체크하도록 수정.

**검증:** `tests/test_main.py`에 "호가 없어도 거래량은 유지"/"거래량 없어도 스프레드는 유지"/"acml_vol='0'은 유효값(None 아님)" 테스트 3개 추가, 기존 테스트도 dict 접근으로 갱신. 전체 179개 통과. 관측 루프만 재시작(이 변경은 `main.py`/`poll_expiry_liquidity` 전용 — COCKPIT/db.py는 무관), 재시작 후 크래시 없이 정상 폴링 확인(로그의 `NumericValueOutOfRange`는 2026-07-06부터 알려진 별개의 얇은 종목 IV 오버플로 이슈로 이미 격리 처리돼 있어 루프 자체는 안 죽음).

**다음 확인 필요:** 수정 후에도 위클리(목) 누적거래량이 계속 0이면 "오늘 그 행사가들에 정말 체결이 없다"는 뜻이고, 0이 아닌 값이 뜨기 시작하면 이번 수정이 실제 원인이었다는 뜻 — 다음 몇 사이클 관찰 필요([[NEXT_TODO]] 참고).

---

## [2026-07-10] 화석 `series='weekly'` 179건 DB에서 완전 삭제

**트리거:** 바로 아래 세션에서 COCKPIT 노출은 코드 필터로 막았지만 DB에는 179건이 그대로 남아있다고 보고했더니, 사용자가 "완전히 지워"라고 명시적으로 요청.

**실행:** `docker exec mahdi_timescaledb psql -U mahdi -d mahdi -c "DELETE FROM expiry_liquidity_1m WHERE series='weekly';"` — 삭제 전 179건 재확인 후 실행, `DELETE 179` 확인. 삭제 후 `series`별 집계로 `regular`/`weekly_mon`/`weekly_thu` 세 값만 남았음을 확인.

---

## [2026-07-10] 위클리 분리 반영 후 재시작 → 화석 "weekly" 행 발견·차단, N/O=월·L/M=목 실측 재확인

**트리거:** 바로 아래 세션의 위클리 월/목 분리를 반영하려고 재시작한 뒤, 사용자가 COCKPIT
스크린샷으로 표에 "먼슬리/weekly/위클리(월)" 3행이 뜨는 걸 보여주며 "두 번째 행 weekly의
정체가 위클리(목)인지 확인하고, 맞다면 나머지 컬럼(특히 잔존일수)이 올바른지 확인해"라고 요청.

**조사:** `docker exec mahdi_timescaledb psql`로 `expiry_liquidity_1m`을 직접 조회한 결과,
`series='weekly'`(따옴표 없는 옛 병합 라벨) 행이 179건 남아 있고 최신 타임스탬프가 10:16(재시작
이전, 즉 위클리 분리 반영 전 마지막 폴링) — **이 "weekly" 행은 위클리(목)이 아니라, 분리 전
구코드가 남긴 화석 데이터**였다(재시작 후 새 코드는 더 이상 "weekly"를 쓰지 않으므로 이후로도
안 갱신되고 `DISTINCT ON (series)`에 계속 잡혀 사라지지 않는 문제 — Flow Radar의
`_LEGACY_MIXED_SYMBOL`과 동일한 화석 데이터 패턴). 실제 `weekly_thu` 행은 첫 폴링 사이클(10:32)엔
그 사이클만 REST 500으로 실패해 빠졌다가 다음 사이클(10:37)에 정상 적재됨을 확인 — expiry
2026-07-16(목요일), 잔존일수 6(2026-07-10 기준 정확), 스프레드 41.36%. **이 실측이 `get_quote()`
기반 실제 만기일로 N/O=위클리(월)/L/M=위클리(목) 매핑을 다시 한번 교차검증**해줬다(COCKPIT
표시값 추론에 이어 REST 데이터로도 확정 — [[NEXT_TODO]]의 남은 확인 항목 해소).

**구현:** `mahdi/data/db.py`의 `latest_expiry_liquidity()`에 `_VALID_EXPIRY_LIQUIDITY_SERIES =
("regular", "weekly_mon", "weekly_thu")` 상수를 추가하고 쿼리에 `series = ANY(%s)` 필터를 넣어,
코드가 더 이상 쓰지 않는 옛 series 값이 DB에 남아 있어도 COCKPIT에 다시 나타나지 않게 했다.
`tests/test_data_db.py`에 필터 파라미터 검증 테스트 2개 추가, 전체 176개 통과. COCKPIT만
재시작(이 수정은 대시보드 읽기 경로만 바꾸므로 관측 루프는 그대로 유지) — 재시작 과정에서
PowerShell로 새 콘솔을 띄우다 `PYTHONUTF8` 환경변수 값이 깨지는 시행착오가 있었으나, 표준
배치파일과 동일한 문법(`set PYTHONUTF8=1` 별도 줄)으로 재시도해 정상화.

**다음 확인 필요:** DB에 남은 179건의 화석 `series='weekly'` 행은 코드 수정으로 COCKPIT에는
더 이상 안 보이지만 실제로 지우고 싶으면 `DELETE FROM expiry_liquidity_1m WHERE series='weekly'`
가 필요 — 파괴적 작업이라 사용자 확인 후 진행할 것.

---

## [2026-07-10] 위클리를 월/목 두 북으로 분리 (COCKPIT이 실측으로 N/O="위클리(월)" 확정해준 덕)

**트리거:** 바로 아래 세션에서 고친 COCKPIT 만기유동성비교 패널을 사용자가 직접 열어보고 스크린샷으로 결과를 보여줌 — 위클리 행이 만기 2026-07-13(월요일)로 하나만 떠 있는 걸 보고 "위클리를 월요일 위클리와 목요일 위클리로 분리해서 표시해" 요청.

**중요한 부수 확인:** 이 스크린샷 자체가 바로 아래 세션에서 남겨둔 미해결 질문("N/O·L/M 중 어느 쪽이 월/목인지 실측 필요")에 대한 답이 됐다 — 표시된 만기 2026-07-13은 실제로 월요일이고, 그 값은 당시 더 가까운 쪽이던 N/O 풀에서 나온 것이므로 **N/O=위클리(월), L/M=위클리(목)**로 확정. 이는 2026-07-06에 eFriend에서 발견했던 "KOSPI200 위클리(월)"/"위클리(목)" 별도 상품 분리와 정확히 일치 — "N/O↔L/M이 같은 상품의 교대 코드풀"이라는 전날 가설을 "서로 다른 두 상품"으로 정정.

**구현:**
- `symbol_master.py`: `_SERIES_PRODUCT_TYPES`를 병합된 `"weekly"` 하나에서 `"weekly_mon"`(N/O)·`"weekly_thu"`(L/M) 두 개로 분리. 관련 주석·docstring 전부 갱신.
- `main.py`: `weekly_manager` 하나를 `weekly_mon_manager`/`weekly_thu_manager` 둘로 분리, `books`/`run_observation_loop`에 3북 전달. WS 슬롯 예산 재계산 결과 ATM±3 유지 시 14×3+1=43으로 `MAX_SUBSCRIPTIONS`(41) 초과 — 사용자에게 "먼슬리만 ±3 유지"와 "세 북 모두 ±2 통일" 중 선택지를 물어 후자로 결정(연구문서 권장안과 동일). `STRIKES_EACH_SIDE`를 3→2로 낮춰 10×3+1=31/41로 확정.
- `expiry_liquidity_panel.py`/`data_source.py`/`app.py`: `_SERIES_LABEL_KO`에 "위클리(월)"/"위클리(목)" 추가, 먼슬리 만기 주 안내 문구를 "위클리(목)만 영향받고 위클리(월)은 그대로"로 정교화, 합성 폴백 스냅샷에도 위클리(목) 행 추가.
- `db.py`/`005_expiry_liquidity.sql`: 주석만 갱신 — `series` 컬럼이 VARCHAR(10)인데 "weekly_mon"/"weekly_thu" 둘 다 정확히 10자라 스키마 변경 없이 그대로 수용됨.

**검증:** `test_data_symbol_master.py`를 weekly_mon/weekly_thu 개별 테스트로 재구성, `test_dashboard_expiry_liquidity_panel.py`에 3행(먼슬리+위클리월+위클리목) 케이스로 갱신. 전체 174개 통과.

**다음 확인 필요:** N/O=월/L/M=목 매핑은 COCKPIT 표시값 하나로 추론한 것이라, 실거래 중 `get_quote()`의 `futs_last_tr_date` 요일로 한 번 더 교차검증 권장 — [[NEXT_TODO]] 참고. 재시작 전까지는 `mahdi.main`이 옛 `weekly_manager`(단일, ATM±3) 로직으로 계속 돈다.

---

## [2026-07-10] 위클리 콜/풋 코드풀(N/O만 조회하던 버그) 발견·수정 + 먼슬리 만기 주 COCKPIT 안내 추가

**트리거:** 사용자가 eFriend에서 캡처한 목요일 위클리 콜/풋 종목코드 화면(C09F6WA69=풋/B09F6WA69=콜, "위클리P/C 2607W3")을 첨부하며 "어제(목요일)는 먼슬리 만기 주라 위클리가 먼슬리 가격을 대신 보여줬다(대시됐다)"고 알려주고, 이 내용을 만기유동성비교·Flow Radar에 반영해달라고 요청.

**조사:** 캡처만으로는 두 코드가 콜/풋 중 뭔지, "대시"가 어디서 관측된 현상인지 불명확해 먼저 `AskUserQuestion`으로 확인(→ 먼슬리 만기 주엔 목요일 위클리 신규 상장이 없고 먼슬리가 대신한다는 뜻, 두 코드는 하나는 콜·하나는 풋, 원하는 반영 방식은 "먼슬리 만기 주 판별 로직 추가"). 이후 `symbol_master.load_index_derivatives_master()`로 KIS 마스터파일을 실제로 내려받아 직접 조사:
- 정규 먼슬리 최근월이 이미 202608로 넘어가 있고 위클리 쪽엔 이번 주(7/9, 먼슬리 만기 목요일) 몫이 아예 없음 → "먼슬리 만기 주엔 위클리 미상장" 확인.
- **더 크게 중요한 발견**: 상품종류 `L`/`M`에 "위클리C/P 2607W3"(102 strikes/side) 행이 실제로 존재했다 — 2026-07-06 당시 "L/M은 단 한 행도 없다"고 결론 낸 전제가 틀렸음(그날은 그 주 위클리가 우연히 N/O 풀이었을 뿐). `symbol_master.py`의 `options(series="weekly")`는 N/O만 필터링하므로, L/M 풀이 그 주의 활성 위클리일 때는 최근접 위클리를 통째로 못 찾는 버그였다 — "위클리 시세가 대시됐다"의 실제 원인 후보. 이 발견은 NEXT_TODO의 미해결 항목("위클리(목)"이 N/O를 공유하는지 새 상품종류를 쓰는지 확인 필요, 2026-07-06)과도 직접 연결됨 — L/M이 그 "위클리(목)" 상품일 가능성이 높으나, 정확한 만기 요일(월/목) 대응은 실측 `get_quote()` 없이는 이름만으로 확정 불가(기존 코드 주석과 동일한 제약).

**구현:**
- `mahdi/data/symbol_master.py`: `_SERIES_PRODUCT_TYPES["weekly"]`가 콜/풋 각각 상품종류 코드 튜플(`("N","L")`/`("O","M")`)을 갖도록 확장, `options()` 필터를 `==`에서 `.isin(...)`으로 변경. `PRODUCT_TYPE_WEEKLY_OPTION_CALL_ALT`("L")/`PUT_ALT`("M") 상수 추가, 2026-07-06 주석의 "L/M 없음" 결론을 정정하는 주석으로 교체.
- `mahdi/dashboard/panels/expiry_liquidity_panel.py` + `app.py`: `build_expiry_liquidity_table(rows, today=...)`에 `_is_monthly_expiry_week()` 판정 추가 — regular 북 만기가 오늘과 같은 ISO주에 속하면 표 제목에 "이번 주는 먼슬리 만기 주 — 위클리 신규 상장 없음" 안내를 표시. `app.py`는 `snapshot.as_of.date()`를 넘겨줌.
- Flow Radar(`data_source.py`)는 별도 수정 없음 — "가장 활발한 옵션" 선정이 `master.option_symbol()`을 거치는 `RollingSubscriptionManager` 구독에 의존하므로 위 심볼마스터 수정으로 자동 해결됨.

**검증:** `tests/test_data_symbol_master.py`에 L/M 풀 픽스처 행 추가 + 두 풀이 함께 잡히는지/두 풀 중 진짜 최근접만 남는지 테스트 갱신, `tests/test_dashboard_expiry_liquidity_panel.py`에 먼슬리 만기 주 안내 노출/비노출 테스트 2개 추가. 전체 172개 통과(주의: 이 PC의 기본 `python`은 `anaconda3/envs/py37_32`(3.7)를 가리켜 `Path.write_text(newline=...)` 등에서 즉시 실패함 — 반드시 프로젝트 `.venv/Scripts/python.exe`로 실행할 것, 이미 알려진 사항이나 이번에도 처음에 걸림).

**다음 확인 필요:** L/M 풀이 정말 "위클리(목)"이고 N/O가 "위클리(월)"인지(또는 단순 격주 교대 풀인지) 실거래 중 `get_quote()`의 `futs_last_tr_date`로 요일 확정 필요 — [[NEXT_TODO]] 참고.

---

## [2026-07-09] Gamma Wall 5분 간격 4분 유실 조사 → 폴링 3개 루프 고정 틱 스케줄링 전환

**트리거:** 사용자가 COCKPIT의 Gamma Wall을 보다가 09:03/09:08/09:13/09:18 — 정확히 5분 간격으로 `option_analysis_1m`의 특정 분이 통째로 비는 패턴을 발견해 "이득/손실을 조사하고, 개선할 경우 방향을 설명해"라고 요청.

**조사:** `mahdi/main.py`의 `poll_option_chain`/`poll_expiry_liquidity`/`poll_investor_flow`와 `mahdi/broker/rest_client.py`의 `_RateLimiter`를 코드로 직접 분석. 두 겹의 원인을 확인: ① `poll_option_chain`이 "작업 후 sleep(60)" 패턴이라 사이클 자체 소요시간(~14초, 28콜×0.5초 페이싱)이 매 사이클 실제 주기에 그대로 더해져(~74초) `poll_time`(분 단위로 자른 타임스탬프)이 누적 드리프트로 분 경계를 종종 두 번 건너뛴다. ② `poll_expiry_liquidity`(300초 주기, ~11콜)가 같은 `KISRestClient`의 공유 `_RateLimiter` 큐에 5분마다 끼어들어 그 순간의 `poll_option_chain` 사이클을 추가로 늦춰, 드리프트가 분 경계를 넘는 시점이 정확히 5분 간격으로 규칙화됨을 확인 — 유실 규모는 405분 중 4분(~1%)으로, 07-08에 고친 203분(50%) 유실보다 훨씬 작은 잔여 이슈. 사용자에게 이득/손실을 보고한 뒤 "제안된 개선방향으로 구현해" 승인 받음.

**구현:** `mahdi/main.py`의 세 폴러 전부를 "작업 후 interval만큼 sleep"에서 절대시각 `next_tick` 기반 고정 틱 스케줄링으로 전환(`asyncio.get_running_loop().time()` 기준, 사이클이 밀려도 캐치업 안 하고 그 시점으로 재기준). `poll_expiry_liquidity`에 `startup_offset_seconds`(기본 0, `main()`에서 `EXPIRY_LIQUIDITY_STARTUP_OFFSET_SECONDS=30.0` 전달) 추가해 `poll_option_chain`과 정규 사이클이 계속 겹치지 않게 어긋냄. 레이트리미터(`_RateLimiter`, 0.5초/콜) 자체는 07-08에 이미 고친 대형 유실 재현 위험 때문에 건드리지 않음([[DECISION_LOG]] 참고).

**검증:** `tests/test_main.py`에 신규 테스트 4개 추가 — 고정 틱 스케줄이 정상 사이클엔 예정된 간격을, 밀린 사이클엔 즉시 재기준(0초 대기)을 하는지 `asyncio.get_running_loop`를 페이크 시계로 몽키패치해 검증. `startup_offset_seconds` 지정/미지정(기본값 0, 하위호환) 둘 다 검증. 전체 테스트 스위트 170개 전부 통과(`.venv`의 pytest, py37 conda 환경은 `typing.Protocol` 미지원으로 이 프로젝트에 안 맞음 — 반드시 프로젝트 로컬 `.venv`로 실행할 것).

**다음 확인 필요:** 정규장 하루 운영 후 이 5분 간격 유실 패턴이 실제로 사라졌는지 DB(`option_analysis_1m`)로 재확인([[NEXT_TODO]] 참고).

---

## [2026-07-09] 7/8 기동~종료 흐름 점검 → REST 500 대량 유실·로그 인코딩·vollib 빈줄 4건 발견·수정

**트리거:** 사용자가 "7/8 마흐디 기동부터 종료까지 작동 흐름을 점검하고 이상점 및 개선점을 조사해" 요청.

**조사:** `logs/premarket_startup.log`(기동 07:30/종료 15:45 정상), `logs/cockpit.log`(7/8 구간 667,663줄), `logs/observation_loop.log`(토큰 재발급 마커로 7/8 구간 106667~189969행 추출, 83,303줄)를 직접 분석하고 `mahdi_timescaledb`를 DB 쿼리로 교차검증.

**발견 (4건):**
1. **REST 500 대량 유실**: 정규장(09:00~15:44) 405분 중 203분(50%)치 `option_analysis_1m`이 통째로 0건. REST 호출 실패율 옵션체인 38%/수급 36%/유동성 26%, 500 에러가 평균 2.5·최대 17회 연속으로 뭉쳐서 발생 — `poll_option_chain`/`poll_investor_flow`/`poll_expiry_liquidity` 3개 루프가 레이트리밋 없이 동시에 REST를 쏘는 게 원인으로 추정([[DECISION_LOG]] 참고).
2. **배치 로그 mojibake**: `premarket_startup.log`의 taskkill/docker 출력이 깨진 바이트로 남음 — 배치파일이 UTF-8인데 cmd.exe가 시스템 코드페이지(949)로 읽어서 발생.
3. **COCKPIT 로그 빈 줄 폭증**: 7/8 하루치 cockpit.log 667,663줄 중 667,651줄(99.9%)이 빈 줄.
4. (기존에 알려진 항목 재확인) `find_gamma_flip`의 vollib RuntimeWarning — CURRENT_STATE.md "알려진 자잘한 문제"에 이미 기록돼 있었으나 원인 미조사 상태였음.

**빈 줄 원인 규명 과정**: 헤드리스 Chrome으로 실제 브라우저 세션을 흉내내 COCKPIT을 재현(세션 없이는 `render()`가 아예 안 돎을 먼저 확인) → `sys.stdout.write`를 몽키패치해 콜스택을 남기는 트레이서 스크립트로 정확한 발생 지점 특정 → `vollib/ref_python/black_scholes/__init__.py`의 `d1()` 안 `if not denominator: print('')`(디버그 잔재)로 확정. `find_gamma_flip`이 그리드(41점)마다 이 함수를 호출해서, `iv`나 `t_years`가 0에 가까운 레그 하나만 있어도 리런 1회당 수십~수백 줄이 찍힘.

**구현 (4건, 상세 근거는 각 [[DECISION_LOG]] 항목 참고):**
- `mahdi/broker/rest_client.py`: 스레드 안전 공유 레이트리미터(`_RateLimiter`, 기본 2건/초) 추가, 모든 REST 호출이 `_get`/`_post` 단일 진입점을 거치도록 리팩터링.
- `mahdi/main.py`: `poll_option_chain`/`poll_investor_flow`에 사이클 전체 실패 시 5초 후 1회 재시도(`_collect_option_chain_cycle`/`_collect_investor_flow_cycle` 헬퍼로 분리).
- `scripts/start_mahdi_premarket.bat`/`stop_mahdi_marketclose.bat`: `chcp 65001 >nul` 추가.
- `mahdi/features/options_intel.py`: `find_gamma_flip`의 vollib 호출 구간을 `redirect_stdout`+`catch_warnings`로 국소 억제.

**검증:**
- `pytest` 167개 전부 통과(신규 7개: 레이트리미터 페이싱/동시성 2개, 옵션체인/수급 재시도 3개, vollib stdout/warning 억제 2개) — 신규 테스트는 전부 "수정을 일시적으로 되돌리면 실패하는지"까지 역검증함(1차로 만든 vollib stdout 테스트가 정상 레그를 써서 회귀를 못 잡는 거짓 통과였던 것도 이 과정에서 발견해 경계값 레그로 교체).
- 두 배치파일을 실제로 실행 — 수정 전/후 `premarket_startup.log`를 직접 비교해 mojibake가 사라짐을 확인.
- 헤드리스 Chrome으로 COCKPIT을 40초간 재현 — 수정 전 4,112줄(빈 줄 4,099) → 수정 후 8줄(빈 줄 3, 배너뿐)로 확인.
- 실제 `mahdi.main` 관측 루프를 75초 구동해 레이트리미터·재시도 경로가 실거래 파이프라인에서도 정상 동작하고 DB(`option_analysis_1m`)에 계속 적재됨을 확인. 단, 장전(08시대) 시간대라 일부 종목 500이 여전히 관측됨 — 레이트리밋 완화 효과의 정확한 재확인은 정규장 하루 운영 결과로 봐야 함.
- 테스트 중 생성한 모든 프로세스(streamlit/chrome/observation loop 등) 정리 확인, DB/Redis 컨테이너만 정상 유지.

**다음 확인 필요:** 정규장(09:00~15:45) 중 하루 운영 후 `option_analysis_1m` 분당 데이터 공백 비율이 실제로 줄었는지 DB로 재확인.

---

## [2026-07-07] Flow Radar x축 장외 시간공백 제거 (rangebreaks)

**트리거:** 사용자가 COCKPIT 스크린샷을 보여주며 "Flow Radar 시간축이 전날 장마감 후 다음날 장시작
시간공백을 포함하고 있어 시인성이 없다"고 지적 — 옵션 종목처럼 체결이 뜸한 계열은 대부분의 점이
차트 양쪽 끝(전일 장마감 직전/당일 개장 직후)에 몰려 찍히고 그 사이 15~16시간의 빈 공백이 x축의
대부분을 차지해 그래프가 거의 안 보였음.

**수정:** `mahdi/dashboard/panels/flow_radar_panel.py`에 `_TRADING_HOURS_RANGEBREAKS`
(주말 전체 + 매일 15:45~09:00) 상수와 `_apply_trading_hours_rangebreaks()` 헬퍼를 추가,
`build_ofi_sparkline`/`build_vpin_chart`/`build_microprice_vs_price_chart` 세 함수 모두에 적용.
거래시간 09:00~15:45는 v6 §16.1에 이미 고정값으로 문서화돼 있어 그 값을 그대로 재사용함(야간거래
세션 없음 확인). Plotly `rangebreaks`는 실제 데이터 포인트 개수·간격과 무관하게 동작하므로 옵션처럼
점이 희소한 계열에도 그대로 적용 가능.

**검증:** 기존 `tests/test_dashboard_panels.py` 10개 전부 통과(회귀 없음). 전일 15:00~당일 09:00에
걸친 합성 타임스탬프로 `fig.layout.xaxis.rangebreaks`가 의도대로 설정됨을 스크립트로 직접 확인.
**미확인**: 브라우저에서 실제로 공백이 압축돼 보이는지는 COCKPIT 재시작 후 육안 확인 필요
([[NEXT_TODO]] 참고 — 대시보드 하위 모듈 수정 후 COCKPIT 재시작 필요 관행 적용).

---

## [2026-07-07] 07:30 장전 자동 기동 실패 원인 규명·수정, 당일분 수동 기동

**트리거:** 사용자가 "07:30에 기동되지 않았다"고 보고.

**진단:** `schtasks /Query`로 태스크가 07:30:00에 정상 트리거됐으나 50초 만에 반환코드 255로 실패한 것을 확인. `logs/premarket_startup.log`엔 시작 로그 한 줄만 남고 그 뒤가 전혀 없었음. `Get-CimInstance Win32_OperatingSystem`으로 PC `LastBootUpTime`이 07:25:42(트리거 4분 전 갓 부팅)임을 확인 — 세션이 막 준비되는 시점에 트리거된 정황. 이후 사용자 요청으로 `start_mahdi_premarket.bat`을 수동 재실행해 동일하게(즉시, `- was unexpected at this time`) 재현됨.

**근본 원인:** 재부팅 타이밍이 아니라 배치파일 자체의 잠복 버그였음 — `if not exist "%DOCKER_DESKTOP_EXE%" ( ... )` 블록 안 echo문에 `(%DOCKER_DESKTOP_EXE%)`처럼 이스케이프 없는 괄호가 있어, 이 분기가 실제로 실행될 때(=Docker Desktop이 안 켜져 있을 때)만 cmd.exe 파서가 블록을 조기 종료시켜 즉시 구문 에러로 죽었다. 07-06엔 Docker가 이미 켜져 있어 이 분기를 안 타서 안 걸렸을 뿐. [[DECISION_LOG]] 참고.

**수정:** `scripts/start_mahdi_premarket.bat`의 해당 줄 괄호를 `^(...^)`로 이스케이프.

**결과:** 수정 후 재실행해 당일(07-07)분 수동 기동 완료 확인 — Docker Desktop 실행(07:52:50)→데몬 준비(07:53:03)→`docker compose up -d`(mahdi_redis/mahdi_timescaledb Running)→COCKPIT(streamlit)/관측 루프(python) 전부 기동, `docker ps`·프로세스 목록으로 재확인.

---

## [2026-07-06] Phase 1.5 전체(①~④) 구현·실운영 반영 — RESEARCH_EXPIRY_SELECTION_v1 후속

**트리거:** 바로 아래 세션에서 제안한 Phase 1.5 로드맵을 "반영하고 다음 작업을 진행해"라는 요청에 따라 실제 구현.

**구현 내용:**
- ① `symbol_master.py`: 위클리(N/O) 조회 추가 — `options()`/`nearest_expiry_chain()`/`option_symbol()`에 `series="regular"|"mini"|"weekly"` 인자. 실측 결과 KIS 공식 문서(L/M)와 달리 실제 상품종류는 N/O임을 확인(테스트 12개).
- ② `main.py`: `run_observation_loop`/`poll_option_chain`이 리스트(`subscription_managers`)/`books`(매니저,series 튜플)를 받도록 시그니처 변경, `main()`에서 먼슬리+위클리 두 `RollingSubscriptionManager`를 동시 구독(슬롯 29/41).
- ③ `expiry_liquidity_1m` 테이블 신설(`005_expiry_liquidity.sql`) + `poll_expiry_liquidity()` — 사용자가 "정식 스펙대로"(%스프레드 포함) 선택, `get_asking_price()`로 Cao-Wei %스프레드·깊이·거래량 집계. 레이트리밋 우려로 폴링 주기를 옵션체인(60초)보다 긴 300초로 설정.
- ④ `expiry_liquidity_panel.py`(Plotly Table) — COCKPIT에 "만기 유동성 비교(먼슬리 vs 위클리)" 섹션 추가.
- 전체 157→159개 테스트 통과 확인.

**재시작 중 실측 이슈(중요):** 기존 mahdi.main/COCKPIT이 2중 프로세스로 떠 있던 것을 정리 후 재시작했더니, 위클리 옵션의 비정상 IV 값이 `option_analysis_1m.iv DECIMAL(8,6)` 범위를 넘겨 `NumericValueOutOfRange`로 관측 루프 전체가 크래시함 — DB 삽입 실패를 레그/북 단위 `try/except`+`rollback`으로 격리하는 수정을 즉시 추가하고 재재시작해 정상화([[DECISION_LOG]] 참고).

**결과 확인:** `market_raw_1m`에 위클리(BAFB*/CAFB*)·먼슬리(B0160*/C0160*)·선물(A01609) 전부 최신 봉 적재 확인, `expiry_liquidity_1m`에 regular 1행 적재 확인(weekly는 다음 5분 주기 대기).

---

## [2026-07-06] 거래 대상(만기·종목) 선발 체계 조사·제안 — RESEARCH_EXPIRY_SELECTION_v1

**트리거:** 사용자가 (1) Flow Radar 옵션 대상이 무엇인지 질문(→ 현재 정규 월물 근월만 구독, 위클리 N/O는 조회 메서드 자체가 없음을 확인), (2) 향후 진입·청산 대상을 먼슬리+위클리(월/목) 중 활발한 종목으로 해야 하지 않는가, 당일 선발·운용 방법을 학술·기관 문헌으로 조사·제안하라고 요청.

**조사 핵심 근거:**
- KCMI Issue Report 19-11: KOSPI200 최근월물 일평균 거래량이 잔존만기 1~6일 195.6만 > 7~13일 158.0만 > 14~20일 127.2만 > 21~27일 108.6만 계약(전 구간 1% 유의) — 유동성은 최단만기에 구조적 집중. 단 Stoxx50/Nikkei 위클리 실패 사례도 있어 KRX 실측 필수.
- Beckmeyer-Branger-Gayda(SSRN 4404704): 0DTE 개인 방향성 OTM 매수는 문서화된 손실 패턴 → 금지 규칙의 근거.
- CBOE 리서치: 0DTE 딜러 순감마 헤지는 SPX 유동성의 ~0.2%(공포 과장), 그러나 pinning·헤지 플로우 집중은 실재.
- Cao-Wei(2010): 유동성 지표는 달러 스프레드 금지, % 스프레드 사용(만기·머니니스에 의한 기계적 왜곡).
- 기관 관행: 만기 선택은 장전 확정 후 하루 유지가 표준, 일중 만기 교체는 비표준.

**제안 요지(문서: docs/Dev_md/RESEARCH_EXPIRY_SELECTION_v1.md):**
- 선발 단위는 개별 종목이 아니라 **만기 북(book)** — 행사가는 기존 RollingSubscriptionManager가 ATM 추종 롤링(2계층 구조).
- 장전(08:45) 복합 유동성 점수(전일 거래량·OI, ATM±2 % 스프레드, 호가 깊이, 잔존만기)로 주 거래 북 선발, 09:03~05 개장 실측으로 1회 재확인.
- 만기 북은 하루 고정 + 예외 2개: 유동성 강등 트리거(ATM % 스프레드가 20일 동시간대 중앙값 2배를 M분 연속 초과 시 차점 북으로, 하루 1회 한정), 0DTE 플레이북 오버레이(v6 §11.4 기존 규정).
- 진입은 ATM±1~2 한정, 깊은 OTM·OI=0 제외, 0DTE 방향성 네이키드 매수 금지.
- 구현 로드맵: Phase1.5(위클리 N/O 조회 추가 → 2북 동시 구독[슬롯 29/41] → expiry_liquidity_1m 신설 → COCKPIT 패널) → Phase2(선발 스코어러+강등 트리거+북별 파라미터+섀도우 30일).

**정정:** NEXT_TODO의 예전 기록 "위클리는 메서드는 있지만 main.py에서 안 씀"은 부정확 — 실제로는 위클리(N/O) 조회 경로 자체가 없음(미니 D/E만 `mini=True`로 지원). 정정 완료.

---

## [2026-07-06] Flow Radar 개편 2차 — 옵션에도 VPIN 통일 적용 + UI 배치/x축 수정

**트리거:** 사용자가 Flow Radar 분리 스크린샷을 보고 3가지 요청: (1) 옵션 "가장 활발한 종목" 기준 설명 (2) VPIN 차트가 옵션 섹션에 빠져있음 → 검토 후 삽입 (3) 옵션 섹션을 선물 위로 배치 + x축을 선물과 통일.

**설명(질문 응답):** "가장 활발한 종목"은 거래량/체결건수가 아니라 `market_raw_1m`에서 **봉이 가장 최근에 완성된**(`max(timestamp)`) 종목 1개. 평가 주기는 COCKPIT 리런(10초)마다 재실행되지만, 실제 변경은 새 봉이 완성되는 분 단위로만 일어남.

**VPIN 차트 삽입 방식 결정:** 두 옵션(그대로 0 고정 표시 vs 실제 계산) 중 사용자가 "실제로 계산"을 선택 — 옵션에도 VPIN을 적용하기로 확정.

**구현(이전 세션의 "선물 전용 VPIN" 결정을 뒤집음, [[DECISION_LOG]] 참고):**
- `db/migrations/004_active_futures_symbol.sql` 신규(`active_futures_symbol` 레지스트리) — 옵션에도 vpin이 채워지면서 "vpin IS NOT NULL = 선물"이라는 예전 휴리스틱이 깨져, 대시보드가 현재 선물 단축코드를 명시적으로 조회할 방법이 필요해짐. `run_observation_loop`가 구독 직후 이 테이블에 자기 등록.
- `mahdi/main.py`: 선물 전용 특수 분기를 제거하고 `handle_message`를 **종목 구분 없이 통일** — `aggregators`/`volume_buckets`/`vpin_returns`/`vpin_volumes`를 전부 종목별 dict로 관리, 어느 종목이든 봉이 완성되면 그 종목의 VPIN을 계산해 적재. 코드가 오히려 더 단순해짐(중복 분기 제거).
- `mahdi/dashboard/data_source.py`: 선물/옵션 식별을 `get_active_futures_symbol()` 레지스트리 조회로 교체, `option_vpin_series` 필드 추가.
- `mahdi/dashboard/panels/flow_radar_panel.py`: `build_ofi_sparkline`/`build_vpin_chart`/`build_microprice_vs_price_chart`에 선택적 `x_range` 파라미터 추가.
- `mahdi/dashboard/app.py`: "옵션" 섹션을 "선물" 위로 재배치, 옵션 섹션에 VPIN 차트 추가, 선물 시계열의 시작/끝을 옵션 차트에도 `x_range`로 강제 적용(옵션 데이터가 1~2점뿐일 때 Plotly가 마이크로초 단위로 확대되던 문제 수정).

**검증:** pytest 137→142개 전부 통과(신규: 옵션 심볼에도 VPIN이 계산되는 테스트, active_futures_symbol db 헬퍼 테스트, x_range 적용 테스트, data_source 분리 조회 테스트 갱신). 관측 루프+COCKPIT 둘 다 재시작(이미 둘 다 꺼져 있던 상태였음 — 크래시 아니라 이전 대화 중 사용자가 직접 종료한 것으로 보임). 재시작 후 `load_snapshot()` 직접 호출로 `futures_flow_symbol="A01609"`, `option_flow_symbol="C01607B06"`, `active_futures_symbol` 레지스트리 정상 등록 확인. 옵션 VPIN은 아직 등거래량 버킷이 안 닫혀 0.0(사전에 설명한 대로 옵션 거래량이 얇아 나타나는 정상 현상).

---

## [2026-07-06] Flow Radar 선물/옵션 분리 (VPIN 도입의 부작용 수정)

**트리거:** 사용자가 COCKPIT 스크린샷을 보고 "Flow Radar가 전부 선물 데이터인가? 그렇다면 옵션 거래에 영향은 없나" 질문.

**확인:** DB 조회로 최근 20분간 활동을 비교 — 선물(A01609)은 1분마다 빠짐없이 9개 봉, 모든 옵션 종목은 5~20분+ 공백. Flow Radar의 "가장 최근 활동" 단일 선택 로직이 구조적으로 선물만 계속 뽑는다는 걸 실측으로 확인. Phase2(자동매매)가 아직 없어 프로그램적 영향은 없지만, "옵션 자체의 체결강도를 보고 싶다"는 원래 용도는 사실상 사라졌음을 설명.

**조치:** 사용자가 "선물/옵션 둘 다 보여주기" 선택.
- `data_source.py`: `vpin IS NOT NULL`(선물 전용, main.py 설계상 옵션 경로는 vpin 키 자체를 안 넣음)로 선물 계열을, `vpin IS NULL`로 옵션 계열을 각각 독립적으로 조회하도록 분리. 대시보드가 선물의 실제 단축코드(분기마다 바뀜)를 몰라도 이 조건 하나로 식별 가능.
- `DashboardSnapshot`에 `option_flow_symbol`/`option_timestamps`/`option_ofi_series`/`option_price_series`/`option_microprice_series` 추가, `flow_radar_symbol`은 `futures_flow_symbol`로 개명.
- `app.py`: Flow Radar를 "선물(기초자산)"/"옵션(가장 활발한 종목)" 두 서브섹션으로 분리. 이전에 고친 "가장 활발한 옵션" 캡션 문구도 이 참에 자연스럽게 맞게 정리됨.

**운영 조치:** 이번엔 필드 이름 자체가 바뀌어(`flow_radar_symbol`→`futures_flow_symbol`) COCKPIT을 재시작 안 하면 옛 캐시된 `data_source.py`와 새 `app.py`가 필드 불일치로 크래시할 위험이 있어 재시작. 실제로 재시작 직전 옛 프로세스 로그에서 `TypeError: DashboardSnapshot.__init__() got an unexpected keyword argument 'flow_radar_symbol'`가 관찰됨 — Streamlit 모듈 부분 리로드가 불안정하다는 걸 다시 한번 확인([[DECISION_LOG]] 참고). 재시작 후 신규 프로세스는 에러 없이 정상.

**검증:** pytest 136→137개 전부 통과(신규: 선물/옵션 계열 분리 조회 테스트). `load_snapshot()` 직접 호출로 `futures_flow_symbol="A01609"`, `option_flow_symbol="C01607B09"`(서로 다른 실제 종목) 확인.

**부수 발견 (미해결):** `find_gamma_flip`이 매 리런마다 vollib에서 `RuntimeWarning: divide by zero` 출력 — 크래시는 아니지만 원인 미조사, NEXT_TODO에 기록.

---

## [2026-07-06] VPIN 정상화 — 선물 실시간체결가 신규 구독 + 등거래량 버킷 구현

**트리거:** 사용자가 "VPIN 정상화 구현을 검토해줘" 요청.

**검토:** `calculate_vpin()`(`mahdi/features/orderflow.py`)은 이미 구현·유닛테스트까지 돼 있었지만, 그 입력(등거래량 버킷)을 만드는 로직이 어디에도 없었음 — `market_raw_1m.vpin` 컬럼은 스키마에 있지만 `main.py`가 항상 안 채우고 있었음. 사용자에게 "지금 구독 중인 14개 얇은 옵션에 그대로 적용할지, 선물(기초자산)에 적용할지" 물어봄 — 옵션은 오늘 실측 거래량이 분당 1~10계약으로 얇아 VPIN의 유동성 전제와 안 맞는다는 점을 짚었고, 사용자가 "선물에 적용(범위 큼)"을 선택.

**구현:**
- KIS 공식 문서에서 "지수선물 실시간체결가"(H0IFCNT0, API 실시간-010) 필드 레이아웃 확인 — 옵션(H0IOCNT0)과 필드 순서가 다름(가격 idx5, 매도/매수호가 idx34/35).
- `mahdi/data/collector.py`: `VolumeBucketAggregator` 신규 — 시간이 아니라 누적 거래량 기준으로 버킷을 닫아 (시가→종가 수익률, 거래량)을 반환.
- `mahdi/broker/tr_codes.py`: `WS_TR_FUTURES_CONTRACT = "H0IFCNT0"` 추가.
- `mahdi/main.py`: `_parse_futures_tick()`(옵션과 다른 필드 인덱스) 신규. `run_observation_loop`가 옵션 ATM±3 구독에 더해 선물 실시간체결가도 구독하고, 들어오는 틱을 종목코드로 라우팅(선물이면 volume bucket + VPIN 계산 + 자체 1분봉, 아니면 기존 옵션 경로). 선물 1분봉의 `vpin` 컬럼에 그 시점까지의 VPIN을 실어 `market_raw_1m`에 적재.
- `mahdi/dashboard/data_source.py`: `vpin_series`를 더 이상 0.0 하드코딩하지 않고 `market_raw_1m.vpin`을 그대로 읽음(NULL이면 0.0).

**설계 판단:** COCKPIT Flow Radar의 "가장 활발한 실제 종목 자동선택" 로직은 안 건드림 — 선물이 옵션보다 체결이 훨씬 잦아 자연스럽게 대표 종목으로 뽑히므로, 별도 분기 없이도 OFI/price/microprice/VPIN이 전부 선물 기준으로 일관되게 표시됨([[DECISION_LOG]] 참고).

**검증:** pytest 127→136개 전부 통과. 신규 테스트: `VolumeBucketAggregator`(버킷 완성/리셋/0거래량 무시/잘못된 크기), `_parse_futures_tick`(정상/헤더제거/잘못된 포맷), `run_observation_loop`가 실제로 2개 버킷을 닫아 표준편차>0인 VPIN을 계산해 선물 봉에만 싣고 옵션 틱과 안 섞이는지(직접 계산한 기댓값과 일치 확인), `data_source.py`의 vpin 실값/NULL 처리. 관측 루프 재시작(오늘 다섯 번째)은 아직 안 함 — 사용자 확인 대기.

---

## [2026-07-06] COCKPIT "합성 리플레이" 재발 → Streamlit 모듈 캐싱 문제 발견·수정

**트리거:** 사용자가 COCKPIT 스크린샷 공유 — "DB에서 데이터를 찾지 못해 합성 리플레이 데이터로 표시 중" 경고가 다시 뜸.

**조사:** `load_snapshot()`을 직접 호출하면 정상적으로 라이브 데이터가 나오는데, 브라우저의 COCKPIT 화면만 합성 데이터를 보여주는 모순 발견. `logs/cockpit.log`가 아예 없다는 사실에서 COCKPIT 프로세스가 **오늘 한 번도 재시작되지 않았음**(07:30 최초 기동 그대로)을 확인. 원인: Streamlit은 엔트리 스크립트(`app.py`)만 매 리런마다 새로 읽고, `import`된 `data_source.py`는 파이썬 모듈 캐시에 남는다 — 오늘 `data_source.py`를 여러 번 고쳤지만(기초자산 스팟 재설계, 옵션 체인, 수급), 프로세스를 재시작 안 해서 가장 오래된(고정 라벨 `"KOSPI200_OPT"` 조회하던) 버전이 계속 캐싱되어 실행 중이었음 — 그 라벨은 09:09 이후 화석 데이터라 매번 빈 결과 → `None` → 합성 폴백이 반복됐던 것([[DECISION_LOG]] 참고).

**조치:** COCKPIT(Streamlit) 프로세스를 완전히 재시작(오늘 처음)해 모든 모듈을 새로 로드. `data_source.py`의 `except Exception` 폴백 분기에 `logger.exception(...)` 추가 — 다음부터는 이런 상황이 재발해도 `logs/cockpit.log`에서 원인을 바로 추적할 수 있음(예전엔 조용히 `None`만 반환).

**검증:** 재시작 후 `logs/cockpit.log`에 정상 기동 로그(Uvicorn 서버 시작, 에러 없음) 확인. pytest 전체 재실행해 로깅 추가가 기존 폴백 테스트에 영향 없음 확인.

**교훈:** 앞으로 `mahdi/dashboard/` 아래 파일(엔트리 `app.py` 제외하고 `import`되는 모든 모듈)을 고치면, 관측 루프와 별개로 COCKPIT도 반드시 재시작해야 한다.

---

## [2026-07-06] COCKPIT 수급(외국인/기관/개인) 실데이터 연결

**트리거:** 사용자가 COCKPIT 수급 패널이 계속 0인 이유를 질문 → 조사 후 "지금 바로 조사/구현 시작" 요청.

**조사:** `git grep`으로 기존 코드/문서 확인 — `docs/CyBos ref/`(다이신 CYBOS API, 참고용이지 실제 사용 브로커 아님)에 개념적으로 "투자자별 매매동향" API가 있다는 힌트만 확인. KIS 공식 문서(xlsx)를 시트명으로 검색해 **"시장별 투자자매매동향(시세)"**(API ID v1_국내주식-074, TR_ID `FHPTJ04030000`) 발견 — `FID_INPUT_ISCD=K2I`(선물/콜/풋 통합 시장구분), `FID_INPUT_ISCD_2`로 F001(선물)/OC01(콜옵션)/OP01(풋옵션) 구분, 응답에 `frgn_ntby_qty`/`tr_pbmn`(외국인), `prsn_*`(개인), `orgn_*`(기관계) 그대로 존재.

**중요 발견:** 문서엔 "모의 TR_ID/Domain: 모의투자 미지원"이라고 돼 있었지만, 옵션 WS 시세와 같은 패턴("계좌 무관 공개 데이터라 모의 전용 도메인이 없을 뿐")일 수 있다고 판단해 살아있는 모의투자 토큰으로 `REAL_REST_DOMAIN`을 직접 호출 → 200 OK로 실제 수급 수치 확인([[DECISION_LOG]] 참고). 문서만 보고 포기하지 않고 실측으로 확인한 덕분에 기능을 구현할 수 있었음.

**구현:**
- `db/migrations/003_investor_flow.sql`(`investor_flow_1m` 테이블) + 실행 중인 컨테이너에 수동 적용.
- `mahdi/broker/tr_codes.py`: TR_ID/PATH/FID 상수 추가. `mahdi/broker/rest_client.py`: `get_investor_flow()` 추가 — `is_mock` 무관하게 항상 REAL_REST_DOMAIN 사용.
- `mahdi/data/db.py`: `insert_investor_flow`/`latest_investor_flow` 추가.
- `mahdi/main.py`: `poll_investor_flow()` 신규 — 선물/콜/풋 3세그먼트를 순차 조회해 외국인/기관/개인 순매수 거래대금을 합산 후 적재(세션 누적치 스냅샷). 세그먼트 하나 실패해도 나머지로 계속 진행. `main()`의 `asyncio.gather`에 세 번째 태스크로 추가.
- `mahdi/dashboard/data_source.py`: `investor_flow_1m` 최신값을 읽어 `foreign_net`/`institution_net`/`individual_net`에 반영(아직 데이터 없으면 0.0 유지). `position_panel.py`: KIS 응답 단위(원/천원)를 확인 못 해 y축 라벨 "순매수(억원)" → "순매수대금"으로 완화(단위 미확인 상태에서 틀린 단위를 주장하지 않기 위함).

**검증:** pytest 120→127개 전부 통과(신규: rest_client 실전도메인 강제 테스트, poll_investor_flow 합산/부분실패 격리 테스트, db 헬퍼 테스트, data_source 라이브/미폴링 케이스 테스트). 관측 루프 재시작은 사용자 확인 후 진행 예정(오늘 네 번째 재시작).

---

## [2026-07-06] 로그 점검 → Docker Desktop 미기동 발견 → 자동 기동/로그 리다이렉션/COCKPIT 자동갱신 추가

**트리거:** 사용자가 마흐디 기동 이후 로그 확인 및 동작 품질·이상점 점검 요청.

**발견:**
- `logs/premarket_startup.log`가 유일한 로그 파일이며 6줄뿐 — 관측 루프(`mahdi.main`)/COCKPIT의 실제 런타임 로그(WS 수신, DB insert, 예외)는 어디에도 안 남음(`cmd /k` 콘솔 창에만 stdout으로 출력).
- 07:30 기동 시 `docker compose up -d`가 Docker Desktop 미실행으로 실패했는데, 배치파일이 에러레벨 체크 없이 COCKPIT/관측루프를 그대로 새 창에 띄움. 재확인(07:56) 결과 Docker Desktop이 여전히 안 켜져 있었음(TimescaleDB/Redis 컨테이너 미기동).
- `python -m mahdi.main`/streamlit 프로세스는 살아있었으나, 장 시작(9:00) 전이라 아직 1분봉이 완성 안 돼 `db.get_connection()`을 안 건드려서 안 죽고 있었던 것 — 개장 직후 DB 연결 실패로 크래시할 것으로 예상됨(설계상 DB 실패 시 예외가 위로 전파).
- COCKPIT은 DB 예외를 캐치해 합성 리플레이로 폴백하고 화면에 경고를 띄우는 안전장치가 이미 있어 정상 동작 확인.
- COCKPIT은 `render()`를 한 번만 실행하는 구조라 DB에 새 데이터가 쌓여도 브라우저를 수동 새로고침하지 않으면 갱신 안 됨(자동 폴링 없었음).

**조치:**
- Docker Desktop을 수동으로 기동 → 컨테이너 자동 복구(재시작 정책) 확인.
- `scripts/start_mahdi_premarket.bat`: `docker compose up -d` 전에 Docker 데몬 준비 확인 → 없으면 Docker Desktop.exe 실행 → 5초 간격 최대 3분(36회) 폴링 후 진행하는 로직 추가(exe 경로는 `%ProgramFiles%\Docker\Docker\Docker Desktop.exe` 고정, 없으면 경고 로그만 남기고 진행).
- 같은 배치파일의 COCKPIT/관측루프 실행 줄에 `>> logs\cockpit.log 2>&1`, `>> logs\observation_loop.log 2>&1` 리다이렉션 추가.
- `mahdi/dashboard/app.py`에 `time.sleep(10)` → `st.rerun()` 폴링 추가(`REFRESH_INTERVAL_SECONDS`) — 외부 패키지 없이 표준 Streamlit만으로 10초 간격 자동 갱신.

**검증:** 배치파일 로직 3개 분기(데몬 이미 준비됨/Desktop.exe 못 찾음/3분 타임아웃)를 스크래치패드 격리 스크립트로 각각 실행 확인. CRLF 유지 확인(바이트 단위로 CR/LF/CRLF 전부 55로 일치). 기존 pytest 스위트는 `app.py`를 직접 실행하지 않아 새 폴링 루프의 영향 없음 확인.

---

## [2026-07-06] 09:01 실데이터 점검 → 심볼 혼입 버그 + WS 헤더 파싱 버그 발견·수정 (장중 핫픽스)

**트리거:** 사용자가 09:01 이후 실제 봉 데이터 적재 확인 및 동작 품질·이상점 점검 요청.

**발견 1 — 심볼 혼입:** `market_raw_1m`을 직접 조회하니 09:00 전후 봉들의 OHLC가 예: 08:58 open 59.95 → low 39.95(1분 안에 33% 급락 후 그대로 마감) 등 KOSPI200 옵션시장에 있을 수 없는 변동성. 원인 추적 결과 `run_observation_loop`가 ATM±3(콜/풋 최대 14종목) 구독임에도 `MinuteBarAggregator` 인스턴스를 하나만 생성해 서로 다른 옵션 종목의 체결가를 한 봉에 합산하고 있었음(`NEXT_TODO.md`에 "실데이터로 검증한 적 없음"으로 이미 적혀 있던 항목이 실전에서 터진 것). `_parse_tick`도 종목코드 필드를 아예 읽지 않고 버리고 있었음.

**조치 1:** `_parse_tick`이 `tuple[str, Tick]`(종목코드, 틱)를 반환하도록 변경, `run_observation_loop`가 `aggregators: dict[str, MinuteBarAggregator]`로 종목별 분리, DB insert도 종목별 실제 코드 사용. 회귀 테스트 추가(`test_run_observation_loop_keeps_different_symbols_in_separate_bars`).

**발견 2(1을 고치다 파생) — WS 헤더 미제거:** 위 수정을 반영해 관측 루프를 재시작하자마자 매 분 `psycopg.errors.StringDataRightTruncation: value too long for type character varying(20)`로 반복 크래시. KIS WS 실시간 프레임이 `암호화유무|TR_ID|데이터건수|실제데이터(^구분)` 파이프 헤더를 앞에 붙여 오는데, 이를 벗기지 않고 종목코드(0번 필드)를 읽어 헤더+실제코드가 합쳐진 긴 문자열이 `VARCHAR(20)`을 초과. idx1(BSOP_HOUR) 이후 필드는 헤더 뒤에 있어 우연히 정상 정렬됐던 것이라 이전까지 안 들켰음([[DECISION_LOG]] 참고).

**조치 2:** `raw.split("|", 3)[-1]`로 헤더 제거 후 `"^"` 분리하도록 수정(헤더 없는 입력도 안전하게 통과). 회귀 테스트 추가(`test_parse_tick_strips_ws_envelope_header_from_symbol`).

**운영 조치:** 두 수정 모두 장중에 관측 루프 프로세스를 강제 종료 후 재시작해야 반영되므로, 매번 사용자에게 명시적으로 확인받고 진행(자동 승인 정책이 라이브 프로세스 kill을 차단해 첫 시도가 거부됨 → AskUserQuestion으로 재확인 후 진행).

**검증:** pytest 109→111개 전부 통과. 재시작 후 `logs/observation_loop.log`에 토큰 발급·마스터파일·시세조회 200 OK 확인. 실제 정상 봉 적재는 백그라운드 확인 진행 중(다음 세션/SESSION_LOG 갱신 시 결과 반영 필요할 수 있음).

---

## [2026-07-06] COCKPIT "기초자산 현재가" 오표시 발견 → 옵션 체인 REST 폴링 신규 구현 (전면 개편)

**트리거:** 사용자가 COCKPIT 스크린샷을 공유하며 "정상작동 확인해달라"고 요청.

**발견:** 화면의 "기초자산 현재가 35.95"가 KOSPI200 지수가 아니라 옵션 체결가였음. `data_source.py`가 고정 라벨 `symbol="KOSPI200_OPT"`로 `market_raw_1m`을 조회하고 있었는데, 이날 앞서 고친 심볼 분리 수정 이후로는 그 라벨에 아무도 안 써서 09:09에 완전히 멈춘 화석 데이터를 계속 보여주고 있었음(`snapshot.is_live=True`라 경고도 안 뜸). Gamma Map/수급도 항상 비어있거나 0.0이었음.

**조사:** KIS 공식 문서(xlsx)가 인코딩 문제로 파싱이 안 돼서, 대신 살아있는 모의투자 연결로 `get_quote("B01607B38", ...)`를 직접 호출해 응답을 실측 — `output1`에 delta_val/gama/theta/vega/hts_ints_vltl(IV)/hts_otst_stpl_qty(OI)/futs_last_tr_date(만기)가, `output3.bstp_nmix_prpr`에 **어느 옵션을 조회하든 항상** KOSPI200 지수 자체가 들어있음을 확인([[DECISION_LOG]] 참고) — 별도 Greeks API/지수 API 없이 기존 `get_quote()` 하나로 충분했음.

**구현:**
- `db/migrations/002_underlying_spot.sql` 신규(`underlying_spot_1m` 테이블) + 이미 떠 있는 컨테이너에 수동 적용(`docker exec -i ... psql < 파일`).
- `mahdi/data/db.py`: `insert_option_analysis_1m`/`insert_underlying_spot`/`latest_underlying_spot`/`latest_option_chain` 추가.
- `mahdi/main.py`: `_parse_option_quote()`(KIS 응답 → option_analysis_1m 행 + 스팟 파싱, gex는 `options_intel.calculate_gex`로 즉시 계산) + `poll_option_chain()`(WS 구독과 동일한 ATM±3 행사가×C/P를 60초 간격으로 REST 폴링, `asyncio.to_thread`로 블로킹 호출이 WS 루프를 막지 않게 함, 개별 종목 실패는 로그만 남기고 계속 진행) 신규. `main()`에서 `run_observation_loop`와 `asyncio.gather`로 동시 실행.
- `mahdi/dashboard/data_source.py` 전면 개편: 스팟은 `underlying_spot_1m`, Gamma Map 체인은 `option_analysis_1m`(행사가별 콜+/풋- 순 GEX 합산 + `find_gamma_flip`/`gamma_walls` 실계산), Flow Radar는 `market_raw_1m`에서 가장 최근 체결된 실제 종목을 자동 선택(옛 화석 라벨 명시적 제외) + 화면에 "대표 종목" 캡션 표시(`app.py`).

**운영 조치:** 새 기능을 살리려고 관측 루프를 장중에 한 번 더 재시작(오늘 세 번째) — 매번 사용자에게 확인받고 진행. 재시작 직후 실제로 종목 1건에서 500 에러가 났지만 설계대로 로그만 남기고 다음 종목으로 계속 진행됨을 확인(장애 격리 정상 동작).

**검증:** pytest 111→120개 전부 통과(신규: `_parse_option_quote`/`poll_option_chain`/db 헬퍼/`data_source` 라이브 스냅샷 테스트, 전부 2026-07-06 실측 응답을 픽스처로 사용). 재시작 후 실제로 `underlying_spot_1m`(진짜 KOSPI200 지수), `option_analysis_1m`(8개 레그, 그릭스/IV/OI/GEX)에 데이터 적재 확인, `load_snapshot()`을 직접 호출해 스팟/Gamma Flip/Gamma Wall/체인이 전부 정상 조합됨을 확인.

**알려진 한계(다음 단계):** 수급(외국인/기관/개인)은 여전히 0.0 고정(별도 KRX API 미착수), VPIN도 0.0 고정(BVC 미구현), skew_25d/spread_state는 NULL(체인 전체 IV 곡선 필요), rv_5d는 KIS hist_vltl 근사치.

---

## [2026-07-06] 장전/장마감 자동화 스케줄러 등록

**작업:**
- `scripts/start_mahdi_premarket.bat`: DB/Redis 기동 → COCKPIT 대시보드(새 창) → 관측 루프(새 창)
- `scripts/stop_mahdi_marketclose.bat`: 창 제목으로 COCKPIT/관측 루프 프로세스 트리 종료(DB는 유지)
- Windows 작업 스케줄러 태스크 2개 등록: `Mahdi-PreMarket-Startup`(평일 07:30), `Mahdi-MarketClose-Shutdown`(평일 15:45)
- `main.py`에 Ctrl+C 시 트레이스백 없이 종료하는 처리 추가

**트러블슈팅:**
- 배치파일 LF 줄바꿈 → cmd.exe 파싱 에러 → CRLF 변환으로 해결 ([[DECISION_LOG]] 참고)
- `taskkill WINDOWTITLE` 앞뒤 와일드카드 미지원 → 뒤쪽 와일드카드만 사용으로 해결

**검증:** 더미 창(ping -t localhost)을 실제 스크립트와 동일한 방식으로 띄워 기동/종료 스크립트 각각 실행 확인. 실제 서비스(uv/docker) 검증은 못 함(작업 도구 세션 자체의 PATH가 stale이라 우회 불가) — 사용자 실제 터미널에서 최초 수동 실행은 성공 확인(토큰 발급·마스터파일 다운로드·시세조회 200 OK).

---

## [2026-07-05~06] KIS TR ID/필드 검증 + 종목코드 마스터 연동

**트리거:** 사용자가 공식 KIS OpenAPI 문서(docs/efriend/한국투자증권_오픈API_전체문서_*.xlsx)를 제공하며 Phase 1 스캐폴드의 TR ID/필드 순서 검증 요청.

**발견 및 수정한 버그:**
| 항목 | 파일 | 수정 내용 |
|---|---|---|
| 주문 바디 필수 필드 누락 | `mahdi/broker/rest_client.py` | `ORD_PRCS_DVSN_CD="02"` 추가, `ORD_DVSN`→`ORD_DVSN_CD` 이름 수정 |
| WS 시세 도메인 오분기 | `mahdi/broker/tr_codes.py`, `mahdi/main.py` | 모의투자 여부와 무관하게 시세는 항상 실전 WS 도메인(:21000) |
| 옵션체인 엔드포인트 오류 | `mahdi/broker/rest_client.py` | display-board(실전 전용, TR ID도 오기) → `get_quote`/`get_asking_price`로 교체 |
| `_parse_tick` 필드 순서 전면 오류 | `mahdi/main.py` | H0IOCNT0 실측 58필드 포맷으로 재작성(가격 idx2, 체결량 idx9, 호가 idx41/42 등) |

**신규 구현:** `mahdi/data/symbol_master.py` — KIS 종목코드 마스터파일 다운로드·파싱. 실제 파일을 내려받아 검증하는 과정에서 컬럼 순서가 공식 참고 스크립트와 다르다는 것, 옵션의 `월물구분코드`가 만기 순번이 아니라는 것을 추가로 발견해 수정 ([[DECISION_LOG]] 참고).

**부수 발견:** `.gitignore`의 `data/`가 `mahdi/data/`(소스코드)까지 무시하고 있어 `collector.py`/`db.py`/`subscription_manager.py`가 첫 커밋부터 git에 안 잡혀있던 버그 발견·수정.

**결과:** pytest 109개 전부 통과. 실사용자 터미널에서 `mahdi.main` 최초 실행 성공(토큰 발급, WS 접속키, 마스터파일 다운로드, 최근월물 시세조회 전부 200 OK).

---

## [2026-07-05] Phase 1(관측 인프라) 스캐폴드 최초 구현

**트리거:** v6 마스터 블루프린트(PART 21 Build Roadmap) 기준 Phase 1 구현 요청.

**작업:**
- 환경: `git init`, uv+Python 3.12, Docker Desktop 설치, TimescaleDB+Redis(docker-compose)
- DB 스키마: `db/migrations/001_init.sql`(11개 테이블, 5개 하이퍼테이블)
- 피처 사전 v1: `mahdi/features/{orderflow,options_intel,volume}.py`
- Regime Engine v1: `mahdi/engines/regime.py` (GaussianHMM 8-state + 워밍업 폴백)
- KIS 브로커 클라이언트: `mahdi/broker/{token_daemon,ws_client,rest_client,order_state_machine,tr_codes}.py`
- 데이터 레이어: `mahdi/data/{collector,subscription_manager,db}.py`
- COCKPIT v1: `mahdi/dashboard/` (Streamlit, Regime/Gamma Map/Flow Radar/수급 패널)
- `mahdi/main.py` 관측 전용 오케스트레이터

**검증:** pytest 98개 전부 통과, Docker Compose로 TimescaleDB 하이퍼테이블 생성 확인, KIS 모의투자 토큰 발급 실제 성공, Streamlit 앱 `AppTest`로 예외 없이 렌더링 확인.

**커밋:** `ed7294e` "Scaffold Mahdi Phase 1 observation infrastructure"
