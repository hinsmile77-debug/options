# CURRENT_STATE — 마흐디(options) 현재 개발 상태

_최종 갱신: 2026-07-09_

---

## 브랜치 / 환경
- 저장소: 2026-07-05 `git init`, 단일 `master` 브랜치
- Python: 3.12 (uv 관리 가상환경, `.python-version`)
- DB: TimescaleDB(`mahdi_timescaledb`) + Redis(`mahdi_redis`), `docker-compose.yml`
- KIS 계좌: 모의투자(VPS), `.env`에 앱키/시크릿/계좌번호 보관(gitignore, 커밋 안 됨)

---

## Phase 1(관측 인프라) 모듈 현재 상태

### mahdi/features/ — 피처 사전 v1
- `orderflow.py`: OFI(Cont-Kukanov-Stoikov), VPIN(BVC), Microprice, Queue Imbalance, Absorption
- `options_intel.py`: GEX, Gamma Flip(그리드 스캔+선형보간), Gamma Wall, Vanna/Charm 집계, VRP
- `volume.py`: Session/Anchored VWAP, Volume Profile(POC/VAH/VAL), Volume Spike
- 전부 pytest 단위테스트로 known-value 검증 완료

### mahdi/engines/regime.py — Regime Engine v1
- `GaussianHMM`(hmmlearn, 8-state), `n_restarts`회 재시작 후 최고 로그우도 모델 채택(EM 지역해 방지)
- 상태→레짐 라벨 매핑은 rv_ratio/stress/thinning/hurst 기반 결정론적 휴리스틱
- `warmup_fallback()`: 장 초반 데이터 부족 시 전일 마감 레짐+갭 z-score로 대체
- **알려진 한계**: §7.3 입력 피처에 방향(상승/하락) 신호가 없어 TREND_UP/DOWN 구분은 hurst만으로 확정 불가 (테스트에서도 이 둘은 "트렌드 계열"로만 검증)

### mahdi/broker/ — KIS OpenAPI 클라이언트
- `token_daemon.py`: 접근토큰 발급/캐싱/만료 자동 갱신 — 모의투자 실제 토큰 발급 확인됨
- `ws_client.py`: WS 접속키 발급, 구독/해제, 슬롯 41건 한도
- `rest_client.py`: `get_quote`/`get_asking_price`(선물옵션 시세/시세호가), `get_balance`, `submit_order`
- `tr_codes.py`: 전체 TR ID/경로/도메인 — **2026-07-06 공식 KIS 문서로 실측 검증 완료** (docs/efriend 참고)
- `order_state_machine.py`: PENDING→PARTIAL/FILLED/CANCELLED/REJECTED 상태전이 강제

### mahdi/data/ — 데이터 레이어
- `collector.py`: `MinuteBarAggregator` — 틱→1분봉, quality_flag(틱 수 부족 시 저품질)
- `subscription_manager.py`: `RollingSubscriptionManager` — ATM±N 구독 롤링, symbol_formatter가 None 반환 시 해당 strike 스킵
- `db.py`: TimescaleDB 커넥션+upsert 헬퍼(market_raw_1m/feature_store/regime_state/option_analysis_1m/underlying_spot_1m) — 2026-07-06: `insert_option_analysis_1m`/`insert_underlying_spot`/`latest_underlying_spot`/`latest_option_chain` 추가
- `symbol_master.py`: KIS 종목코드 마스터파일(`fo_idx_code_mts.mst`) 다운로드·파싱 — 최근월 선물코드, 옵션 체인(행사가 목록), 행사가→단축코드 조회 제공
  - **주의**: 이 파일의 실제 컬럼 순서는 KIS 공식 참고 스크립트와 다름(월물구분코드/행사가/ATM구분 위치가 다름). 옵션의 만기 판별은 `월물구분코드`가 아니라 `한글종목명`에서 정규식으로 추출한 YYYYMM 사용 — symbol_master.py 헤더 주석에 근거 상세 기록.

### db/migrations/002_underlying_spot.sql — 2026-07-06 신규
- `underlying_spot_1m(timestamp, underlying, spot)` 하이퍼테이블 — REST 폴링이 얻은 KOSPI200 지수 자체(output3.bstp_nmix_prpr)를 저장. `market_raw_1m`은 종목(옵션)별 틱 집계용이라 지수를 담기 부적절해 분리.
- **주의**: 001에 이어 실행되는 새 마이그레이션 파일은 신선한 볼륨(다른 PC 최초 배포 등)에서는 `docker-entrypoint-initdb.d`가 자동 적용하지만, 이미 초기화된 기존 컨테이너에는 자동 적용 안 됨 — `docker exec -i mahdi_timescaledb psql -U mahdi -d mahdi < 새마이그레이션.sql`로 수동 적용 필요.

### mahdi/dashboard/ — COCKPIT v1 (Streamlit)
- Regime/Gamma Map/Flow Radar/수급 패널, DB 데이터 없으면 합성 리플레이로 폴백
- 2026-07-06: `render()` 뒤 `time.sleep(REFRESH_INTERVAL_SECONDS)` → `st.rerun()` 폴링 추가 — 브라우저 수동 새로고침 없이 10초 간격 자동 갱신(외부 패키지 불필요)
- **2026-07-06 데이터 소스 전면 개편** ([[DECISION_LOG]] 참고): `data_source.py`가 예전엔 고정 라벨 `symbol="KOSPI200_OPT"`로 `market_raw_1m`을 조회해 "기초자산 현재가"에 옵션 체결가를 잘못 표시하고 있었음(심볼 분리 수정 이후로는 그 라벨에 아무도 안 써서 완전히 멈춘 화석 데이터가 됨). 수정 후:
  - 기초자산 현재가 = `underlying_spot_1m` 최신값(진짜 KOSPI200 지수)
  - Gamma Map = `option_analysis_1m` 최신 체인 스냅샷(행사가별 콜+/풋- 순 GEX 합산) + `options_intel.find_gamma_flip`/`gamma_walls`로 실시간 계산
  - Flow Radar = `market_raw_1m`에서 가장 최근 체결이 있었던 실제 종목을 자동 선택(화면에 "대표 종목: X" 캡션 표시) — 옛 고정 라벨은 명시적으로 제외
  - 2026-07-06 추가: 수급(외국인/기관/개인)도 `investor_flow_1m`에서 실값을 읽어오도록 연결(아래 `poll_investor_flow` 참고). 축 라벨은 KIS 응답 단위(원/천원) 미확인이라 "순매수(억원)"에서 "순매수대금"으로 완화.
  - 2026-07-06 추가: VPIN도 `market_raw_1m.vpin`에서 실값을 읽음(NULL이면 0.0).
  - **2026-07-06 Flow Radar 선물/옵션 분리** (두 차례 개편): 선물(H0IFCNT0) 구독 추가 직후 "가장 최근 활동" 단일 선택 로직이 선물만 계속 고르는 문제를 사용자가 지적 — 선물은 WS로 거의 매분 체결되는 반면 옵션은 거래가 뜸해 공백이 생기므로 두 계열을 **각각 독립적으로** 조회하도록 분리. `DashboardSnapshot`에 `option_flow_symbol`/`option_timestamps`/`option_ofi_series`/`option_vpin_series`/`option_price_series`/`option_microprice_series` 필드 추가(`flow_radar_symbol`은 `futures_flow_symbol`로 개명). `app.py`는 "Flow Radar — 옵션(가장 활발한 종목)"을 위, "Flow Radar — 선물(기초자산)"을 아래로 배치(사용자 요청), 옵션 섹션에도 VPIN 차트 추가, 옵션 차트 x축을 선물 시계열 범위로 강제 통일(옵션은 데이터가 1~2점뿐일 때 Plotly가 마이크로초 단위로 확대하는 문제 수정).
  - 선물/옵션 식별 방식도 2단계로 진화했다: 처음엔 `vpin IS NOT NULL`(선물만 채워짐 가정)로 구분했는데, 옵션에도 VPIN을 적용하면서 그 가정이 깨져 **`active_futures_symbol` 레지스트리 테이블**(신규, `db/migrations/004`)로 명시적 조회로 교체함([[DECISION_LOG]] 참고).
- **2026-07-06 Streamlit 모듈 캐싱 주의** ([[DECISION_LOG]] 참고): `app.py`(엔트리)만 매 리런마다 디스크에서 새로 읽힌다 — `data_source.py`/`panels/*.py`처럼 `import`되는 하위 모듈은 파이썬 모듈 캐시에 남으므로, 그 파일들을 고치면 `st.rerun()` 폴링이나 브라우저 새로고침만으로는 반영 안 되고 **COCKPIT 프로세스 자체를 재시작**해야 한다. `_load_from_db`의 `except Exception`에 `logger.exception(...)` 추가해 향후 원인 추적 가능하게 함.
- **2026-07-07 Flow Radar x축 장외 시간공백 제거**: `flow_radar_panel.py`의 세 차트(OFI/VPIN/체결가) 전부에 Plotly `rangebreaks` 적용 — 주말 전체 + 매일 15:45~09:00(v6 §16.1 거래시간 09:00~15:45 기준)을 x축에서 건너뛴다. 이전엔 전일 장마감~당일 개장 사이 공백이 x축 대부분을 차지해 체결이 뜸한 옵션 계열이 거의 안 보였음([[SESSION_LOG]] 참고). **COCKPIT 재시작 후 브라우저 육안 확인 아직 안 함**.

### mahdi/main.py 옵션 체인 REST 폴링 — 2026-07-06 신규 (`poll_option_chain`)
- WS 구독(ATM±3, `subscription_manager.desired_strikes`)과 동일한 행사가×콜/풋에 대해 60초 간격으로 `rest_client.get_quote()`를 반복 호출 → `option_analysis_1m`/`underlying_spot_1m` 적재.
- **실측으로 확인한 KIS 필드명**(공식 문서 대신 실제 응답으로 검증, 2026-07-06): `output1.delta_val`/`gama`(그대로 "gama", gamma 아님)/`theta`/`vega`, `output1.hts_ints_vltl`(IV, %), `output1.hist_vltl`(과거변동성, rv_5d 근사로 사용), `output1.hts_otst_stpl_qty`/`otst_stpl_qty_icdc`(OI/OI변화), `output1.futs_last_tr_date`(만기일, YYYYMMDD), `output1.acml_vol`(거래량). **`output3.bstp_nmix_prpr`는 어느 옵션 종목을 조회하든 항상 KOSPI200 지수 자체를 반환** — 별도 지수 조회 없이 옵션 조회에 얹혀 기초자산 스팟을 얻는다.
- `get_quote()`는 동기(블로킹) httpx 호출이라 `asyncio.to_thread`로 실행해 WS 수신 루프(run_observation_loop)를 막지 않음 — `asyncio.gather`로 둘을 동시 실행.
- 개별 종목 조회 실패(예: 500 에러 — 2026-07-06 실운영 중 실제로 1개 종목에서 재현)는 로그만 남기고 다음 종목으로 계속 진행 — REST 폴링 하나 실패로 WS 관측 전체가 죽지 않음.
- **알려진 한계**: skew_25d/spread_state는 아직 계산 안 함(NULL) — 25델타 스큐는 체인 전체 IV 곡선이 필요해 레그 단위 파싱만으로는 부족. rv_5d는 정확한 5일 realized vol이 아니라 KIS hist_vltl 근사치.

### mahdi/main.py 투자자 수급 REST 폴링 — 2026-07-06 신규 (`poll_investor_flow`)
- KIS "시장별 투자자매매동향(시세)"(TR `FHPTJ04030000`, `FID_INPUT_ISCD=K2I`) — 선물(F001)/콜옵션(OC01)/풋옵션(OP01) 3세그먼트를 조회해 외국인/기관계/개인 순매수 거래대금(`frgn_ntby_tr_pbmn`/`orgn_ntby_tr_pbmn`/`prsn_ntby_tr_pbmn`)을 합산 → `investor_flow_1m`에 적재.
- **중요**: 이 API는 문서상 "모의 TR_ID/Domain: 모의투자 미지원"이지만, 계좌 무관 공개 시세성 데이터라 모의투자 앱키로 `REAL_REST_DOMAIN` 호출 시 실측으로 200 OK 확인됨(2026-07-06) — `rest_client.get_investor_flow()`는 `is_mock` 분기 없이 항상 REAL_REST_DOMAIN을 쓴다([[DECISION_LOG]] 참고). 시세 WS와 같은 패턴.
- 이 데이터는 **세션 누적치**(1분간 델타 아님) — 폴링 시점까지의 누적 수급 우위 스냅샷을 그대로 저장.
- 세그먼트 3개 중 일부 실패해도 나머지로 합산 계속(하나 실패했다고 전체를 버리지 않음), 셋 다 실패하면 그 사이클은 적재 스킵.
- **알려진 한계**: 응답 필드(`*_ntby_tr_pbmn`)의 정확한 화폐 단위(원/천원)를 문서로 확인 못 해 COCKPIT 축 라벨에서 구체적 단위 표기를 뺌(`position_panel.py`).

### mahdi/main.py VPIN — 2026-07-06 신규 (`VolumeBucketAggregator` + H0IFCNT0 구독), 종목 구분 없이 통일
- VPIN(Easley-Lopez de Prado-O'Hara, BVC)은 원래 유동성 높은 단일 종목을 전제로 설계된 지표라 처음엔 선물(기초자산)에만 적용했으나, 사용자가 "선물/옵션 둘 다 보여달라"고 요청해 **종목 구분 없이 통일 적용**하도록 재구조화([[DECISION_LOG]] 참고). 옵션은 거래량이 얇아(오늘 분당 1~10계약) 버킷이 느리게 완성되거나 VPIN이 0.5(중립) 근처에 자주 머물 수 있음을 알고 진행.
- `run_observation_loop`가 옵션 ATM±3 구독과 별개로 선물 실시간체결가(H0IFCNT0, `futures_symbol`)를 함께 구독. `_parse_futures_tick`이 별도 필드 인덱스로 파싱(옵션 H0IOCNT0와 필드 순서가 다름 — 가격=idx5, 매도/매수호가=idx34/35, 공식 문서 실측). 구독 직후 `active_futures_symbol` 레지스트리에 현재 선물 단축코드를 등록.
- `mahdi/data/collector.py`의 `VolumeBucketAggregator`(신규) — 시간 기준 `MinuteBarAggregator`와 별개로 등거래량(equal-volume) 버킷을 만들어 `calculate_vpin()`(이미 구현·테스트돼 있던 함수, 지금까지 아무도 안 불렀음) 입력을 생성. 버킷 크기 `VPIN_BUCKET_SIZE=50`은 실거래량 관찰 전까지 쓰는 잠정치(학계 관례 "일평균거래량/50"을 이 모의투자 환경에 아직 적용 못 함).
- `handle_message`는 이제 선물/옵션을 구분하지 않고 **모든 종목**에 대해 aggregator·volume bucket·vpin 히스토리를 종목별 dict로 관리 — 어떤 종목이든 1분봉이 완성되면 그 종목의 VPIN을 계산해 `market_raw_1m.vpin`에 실어 적재한다(예전엔 선물 전용 특수 분기가 따로 있었으나 통합·단순화됨).
- **알려진 한계**: 버킷 크기(50계약)는 미보정 추정치 — 실거래량 패턴 관찰 후 재조정 필요. 옵션은 거래가 뜸해 VPIN이 갱신되는 빈도가 선물보다 훨씬 낮을 수 있음.

### mahdi/main.py — 관측 전용 오케스트레이터
- 기동 시 종목코드 마스터파일 다운로드 → 최근월 선물코드 확정 → REST 시세로 스팟 조회 → ATM 구독 → WS 리슨 루프
- `_parse_tick`: H0IOCNT0(지수옵션 실시간체결가) 실측 필드 인덱스로 파싱(가격=idx2, 체결량=idx9, 매도/매수호가=idx41/42 등)
- 시세 WS는 계좌 무관 공개 데이터라 `MARKET_DATA_WS_DOMAIN`(실전 도메인, :21000) 고정 사용 — 모의투자 전용 시세 도메인 없음
- Ctrl+C 시 트레이스백 없이 깔끔하게 종료(2026-07-06 수정)
- **미구현**: `nearest_expiry_chain()`으로 심볼 목록은 뽑을 수 있지만, 각 심볼에 대해 `get_quote()`를 반복 호출해 `option_analysis_1m`(IV/Greeks/OI)을 채우는 루프는 아직 연결 안 됨
- **2026-07-06 실거래 중 발견·수정한 버그 2건** ([[DECISION_LOG]] 참고):
  1. ATM±3(콜/풋 합 최대 14종목) 구독인데 `MinuteBarAggregator` 인스턴스를 하나만 써서 서로 다른 옵션 종목의 체결가가 한 봉에 뒤섞임(OHLC가 60선→40선으로 33% 급락하는 등 실제 시장에 없는 값으로 관측됨) → 종목별 dict로 aggregator 분리, `_parse_tick`이 종목코드(0번 필드)도 함께 반환하도록 수정.
  2. 1번을 고치는 과정에서 KIS WS 실시간 프레임이 `암호화유무|TR_ID|데이터건수|실제데이터(^구분)` 헤더를 앞에 붙여 온다는 사실이 드러남 — 헤더를 안 벗기고 0번 필드를 읽으니 `market_raw_1m.symbol VARCHAR(20)`을 넘겨 매 분 크래시. `raw.split("|", 3)[-1]`로 헤더 제거 후 `"^"` 분리하도록 수정(idx1 이후 필드는 헤더와 무관하게 원래도 맞았음 — 우연히 안 들켰던 것).

### 스케줄러(Windows 작업 스케줄러)
- `scripts/start_mahdi_premarket.bat` + `Mahdi-PreMarket-Startup` 태스크: 평일 07:30, DB/Redis+COCKPIT+관측루프 기동
- `scripts/stop_mahdi_marketclose.bat` + `Mahdi-MarketClose-Shutdown` 태스크: 평일 15:45, COCKPIT+관측루프만 종료(DB는 유지)
- **배치파일은 반드시 CRLF 줄바꿈이어야 함** — LF만 있으면 cmd.exe 파싱이 깨짐(2026-07-06 실제로 겪음)
- 배치파일 내부는 `%~dp0` 기준 상대경로로 프로젝트 루트를 계산 — 절대경로 하드코딩 없음(멀티 PC 이식성).
  단, 스케줄러 Action 등록 자체는 Windows 제약상 절대경로 필요 → PC별 1회 등록 절차로 분리
- 2026-07-06: `docker compose up -d` 실행 전 Docker 데몬 준비 여부를 확인하고, 없으면 `Docker Desktop.exe`를 직접 실행한 뒤 5초 간격 최대 3분 폴링하는 로직 추가(당일 07:30 기동 시 Docker Desktop이 안 켜져 있어 DB/Redis 없이 COCKPIT/관측루프만 뜬 사고 재발 방지). COCKPIT/관측루프 실행 줄에 `logs/cockpit.log`, `logs/observation_loop.log` 리다이렉션도 추가 — 이전엔 런타임 로그가 콘솔 창에만 출력되고 파일에 안 남았음.
- 2026-07-07: 위 07-06 수정으로 추가된 "Docker Desktop 미존재 경고" 분기(if 블록 안 echo)에 이스케이프 안 된 괄호가 있어, 이 분기가 실제로 실행될 때(Docker 꺼진 채 07:30 트리거) cmd.exe가 `- was unexpected at this time`으로 즉시 파싱 실패하는 버그 발견·수정(`^(...^)`로 이스케이프). PC가 트리거 4분 전(07:25:42)에 막 부팅된 상태였음. [[SESSION_LOG]]/[[DECISION_LOG]] 참고.

### db/migrations/004_active_futures_symbol.sql — 2026-07-06 신규
- `active_futures_symbol(underlying, symbol, updated_at)` — 단일 현재값 레지스트리(하이퍼테이블 아님). 대시보드가 "이 종목이 지금 구독 중인 선물인지"를 vpin 유무 같은 휴리스틱 없이 바로 조회하게 함.

### 테스트
- `uv run pytest` — 170개 전부 통과 (2026-07-09 기준). **주의**: 이 PC의 기본 `python`(conda `py37_32`, 3.7)은 `typing.Protocol` 미지원이라 `tests/test_main.py` 임포트부터 실패한다 — 반드시 프로젝트 로컬 `.venv/Scripts/python.exe -m pytest`로 실행할 것.

### 2026-07-09 REST 폴링 안정화 (7/8 하루치 실측 기반, [[SESSION_LOG]]/[[DECISION_LOG]] 참고)
- `mahdi/broker/rest_client.py`: `KISRestClient`에 스레드 안전 공유 레이트리미터(기본 2건/초) 추가 — 옵션체인/수급/유동성 폴링 3개 루프가 동시에 REST를 쏘면서 KIS 앱키 TPS 한도를 넘겨 정규장 405분 중 203분치 `option_analysis_1m`이 통째로 유실됐던 문제 대응.
- `mahdi/main.py`: `poll_option_chain`/`poll_investor_flow`에 사이클 전체 실패 시 5초 후 1회 재시도 추가(`CYCLE_RETRY_BACKOFF_SECONDS`).
- (해소, 2026-07-09 2차 수정) 레이트리미터 도입 후에도 남아있던 잔여 유실(5분 간격, 405분 중 4분) — `poll_option_chain`/`poll_expiry_liquidity`/`poll_investor_flow` 세 루프를 "작업 후 sleep"에서 절대시각 고정 틱(`next_tick`) 스케줄링으로 전환, `poll_expiry_liquidity`에 `startup_offset_seconds=30.0`(`EXPIRY_LIQUIDITY_STARTUP_OFFSET_SECONDS`) 추가해 `poll_option_chain`과의 레이트리미터 큐 충돌 빈도를 낮춤([[DECISION_LOG]] 참고).
- **다음 확인 필요**: 정규장 하루 운영 후 데이터 공백 비율(원래의 대량 유실 + 이번 5분 간격 잔여 유실 둘 다)이 실제로 줄었는지 DB로 재확인(NEXT_TODO 참고).

### 알려진 자잘한 문제
- (해소, 2026-07-09) `find_gamma_flip`의 vollib RuntimeWarning/빈 줄 출력 — 원인은 `vollib.ref_python.d1()`의 디버그용 `print('')`(iv/t_years=0 경계 조건). `redirect_stdout`+`catch_warnings`로 국소 억제 완료.
