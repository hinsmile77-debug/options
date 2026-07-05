# CURRENT_STATE — 마흐디(options) 현재 개발 상태

_최종 갱신: 2026-07-06_

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
- `db.py`: TimescaleDB 커넥션+upsert 헬퍼(market_raw_1m/feature_store/regime_state)
- `symbol_master.py`: KIS 종목코드 마스터파일(`fo_idx_code_mts.mst`) 다운로드·파싱 — 최근월 선물코드, 옵션 체인(행사가 목록), 행사가→단축코드 조회 제공
  - **주의**: 이 파일의 실제 컬럼 순서는 KIS 공식 참고 스크립트와 다름(월물구분코드/행사가/ATM구분 위치가 다름). 옵션의 만기 판별은 `월물구분코드`가 아니라 `한글종목명`에서 정규식으로 추출한 YYYYMM 사용 — symbol_master.py 헤더 주석에 근거 상세 기록.

### mahdi/dashboard/ — COCKPIT v1 (Streamlit)
- Regime/Gamma Map/Flow Radar/수급 패널, DB 데이터 없으면 합성 리플레이로 폴백
- `streamlit.testing.v1.AppTest`로 예외 없이 렌더링 확인

### mahdi/main.py — 관측 전용 오케스트레이터
- 기동 시 종목코드 마스터파일 다운로드 → 최근월 선물코드 확정 → REST 시세로 스팟 조회 → ATM 구독 → WS 리슨 루프
- `_parse_tick`: H0IOCNT0(지수옵션 실시간체결가) 실측 필드 인덱스로 파싱(가격=idx2, 체결량=idx9, 매도/매수호가=idx41/42 등)
- 시세 WS는 계좌 무관 공개 데이터라 `MARKET_DATA_WS_DOMAIN`(실전 도메인, :21000) 고정 사용 — 모의투자 전용 시세 도메인 없음
- Ctrl+C 시 트레이스백 없이 깔끔하게 종료(2026-07-06 수정)
- **미구현**: `nearest_expiry_chain()`으로 심볼 목록은 뽑을 수 있지만, 각 심볼에 대해 `get_quote()`를 반복 호출해 `option_analysis_1m`(IV/Greeks/OI)을 채우는 루프는 아직 연결 안 됨

### 스케줄러(Windows 작업 스케줄러)
- `scripts/start_mahdi_premarket.bat` + `Mahdi-PreMarket-Startup` 태스크: 평일 07:30, DB/Redis+COCKPIT+관측루프 기동
- `scripts/stop_mahdi_marketclose.bat` + `Mahdi-MarketClose-Shutdown` 태스크: 평일 15:45, COCKPIT+관측루프만 종료(DB는 유지)
- **배치파일은 반드시 CRLF 줄바꿈이어야 함** — LF만 있으면 cmd.exe 파싱이 깨짐(2026-07-06 실제로 겪음)
- 배치파일 내부는 `%~dp0` 기준 상대경로로 프로젝트 루트를 계산 — 절대경로 하드코딩 없음(멀티 PC 이식성).
  단, 스케줄러 Action 등록 자체는 Windows 제약상 절대경로 필요 → PC별 1회 등록 절차로 분리

### 테스트
- `uv run pytest` — 109개 전부 통과 (2026-07-06 기준)
