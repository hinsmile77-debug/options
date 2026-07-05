# SESSION_LOG — 대화별 작업 이력

_최신 세션이 위에 오도록 역순 정렬_

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
