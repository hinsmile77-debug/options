# SESSION_LOG — 대화별 작업 이력

_최신 세션이 위에 오도록 역순 정렬_

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
