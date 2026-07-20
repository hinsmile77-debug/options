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
- [x] **`cross_asset_stress` 실계산 배선 완료(2026-07-20)** — 더 이상 0.0 고정 스텁이 아니다.
      `mahdi/features/regime_features.py`의 `cross_asset_stress(usdkrw_daily_series,
      usdcnh_recent_series, us10y_daily_series)`가 세 시퀀스 각각에 `book_thinning`과 동일한
      z-score(`_series_zscore`로 추출·공유)를 구해 절대값 평균을 낸다. `RegimeStateMachine.step`이
      `db.recent_usdkrw_daily_series`(일봉, 최근 10거래일)·`db.recent_usdcnh_series`(5분 스냅샷,
      최근 24개=2시간)·`db.recent_us10y_daily_series`(일봉, 최근 10거래일)를 매 선물봉마다 조회해
      넘긴다. USDKRW/US10Y는 거래일당 값이 하나뿐이라 "급변"이 거래일 단위 day-over-day 변화로
      측정되고, USDCNH는 5분 주기라 인트라데이 변동을 그대로 반영 — 이 refresh 주기 차이를 그대로
      살린 설계다. `feature_store`에 실제로 유의미한 값이 쌓이는지는 정규장 운영 후 확인 필요.
- [x] **`macro_score`(`compute_macro_score_proxy`) 복합 신호로 확장 완료(2026-07-20)** — 함수
      이름·시그니처는 [[DECISION_LOG]] 2026-07-10 결정("시그니처는 유지하고 내부만 교체")대로
      유지, 내부 계산만 교체함. 기존 외국인 순매수 부호(K-market 수급)에 4개 신호 추가:
      VIX 기간구조 부호(백워데이션=위험회피/콘탱고=위험선호), USDKRW/USDCNH 추세(상승=자국통화
      약세=위험회피로 부호 반전), ES(S&P500 선물) 추세(상승=위험선호). 각 신호는 -1/0/+1로
      정규화해 "존재하는 신호만" 평균(데이터 없는 신호는 분모에서도 제외). US10Y·MOVE는 방향이
      위험선호/회피로 명확히 매핑되지 않아(수익률·변동성 급등이 맥락에 따라 다름) 제외 —
      그 "급변 크기"는 이미 `cross_asset_stress()`가 별도로 포착 중. `db.recent_es_front_series`
      신규 추가(`recent_usdcnh_series`와 동일 패턴). `mahdi/engines/regime_pipeline.py`에
      `_directional_sign`/`_trend_sign` 헬퍼 추가.
- [ ] `rv_ratio`(RV5d/RV20d)는 선물 심볼이 분기 롤오버될 때마다 일별 종가 이력이 끊긴다(현재 심볼
      기준으로만 `daily_closes` 조회) — 롤오버 연속성 처리 필요 여부 검토.

## Cross-asset stress 매크로 스냅샷 — 2026-07-10 구현, ZN은 2026-07-20 yfinance 폴백으로 전환

- [x] **CBOT EGW00552 원인 확정** — 2026-07-20 HTS [7936](해외선물옵션 거래소 실시간시세신청/조회)
      실측 확인: CME|CBOT는 **API(무료) 탭에 아예 없고 API(유료) 탭에만 있음** — 기간이용료
      **월 228.8불**. "2026-07-10 신청 완료"로 믿었던 것은 무료 탭 기준이었을 가능성이 높고, 실제로는
      유료 구독이 성립한 적이 없었던 것으로 보임(그래서 6일 넘게 EGW00552가 계속 남).
- [x] **당분간 KIS CBOT 유료 구독 안 함 — yfinance 폴백으로 대체(2026-07-20)** — 모의투자 개발
      단계에서 월 228.8불을 지불하는 건 시기상조라는 사용자 결정. `mahdi/data/yfinance_fallback.py`
      신규(`ZN=F` yfinance), `main.py._collect_macro_snapshot_cycle`이 KIS 조회 실패 시에만 호출.
      `macro_snapshot_5m.zn_front_source`("kis"|"yfinance_fallback"|NULL, migration 010)로 출처
      구분 — COCKPIT [_cbot_status_check](mahdi/dashboard/data_source.py)·
      [macro_panel](mahdi/dashboard/panels/macro_panel.py)에 "(폴백)" 표시로 실제 CBOT 체결가와
      혼동되지 않게 함. **정식 운영 전환 시(또는 안정화 후 유료 구독 결정 시) 재검토 필요** — KIS가
      나중에 성공하기 시작하면 코드 변경 없이 자동으로 KIS 우선 사용된다.
- [x] **v6 "글로벌 확인 신호" 나머지 항목(S&P500 선물·MOVE·USDKRW) 점검 및 수집 배선(2026-07-20)** —
      마스터파일 실측으로 셋 다 KIS 수집 가능 여부를 확정함:
      - **USDKRW**: KIS `frgn_code.mst` 실측 확인 — 환율구분 `FID_COND_MRKT_DIV_CODE="X"`,
        심볼 `FID_INPUT_ISCD="FX@KRW"`로 US10Y와 **완전히 동일한 무료 엔드포인트**
        (`inquire-daily-chartprice`)에서 계좌 게이트 없이 바로 얻어짐 → KIS로 즉시 구현
        (`macro_snapshot_5m.usdkrw`, us10y_yield와 동일하게 일봉 LOCF).
      - **ES(S&P500 E-mini 선물)**: KIS `ffcode.mst` 실측 확인 — 상품코드 `ES`/거래소코드 `CME`
        (ZN의 CBOT와 다른 서브거래소지만 HTS [7936]상 "CME|CME"도 동일하게 유료 월 228.8불) →
        ZN과 동일한 yfinance 폴백 패턴(`ES=F`) 적용(`macro_snapshot_5m.es_front`/`es_front_source`).
      - **MOVE(ICE BofA MOVE Index)**: 마스터파일 어디에도 없음(장외 파생 인덱스라 거래소 상장
        상품 자체가 아님) → KIS 경로 없음, yfinance(`^MOVE`) 전용 폴백만 가능
        (`macro_snapshot_5m.move_index`/`move_index_source`, 항상 "yfinance_fallback").
      `zn_fallback.py`는 3개 심볼(ZN/ES/MOVE)을 공용으로 다루는 `mahdi/data/yfinance_fallback.py`
      (`fetch_last_close(symbol)`)로 일반화함, migration 011.
      **참고**: `cross_asset_stress()` 실계산 배선과 `macro_score`(`compute_macro_score_proxy`)에
      ES 추세 반영까지 같은 날 바로 이어서 완료함(위 레짐 절 항목 참고). MOVE는 방향성이
      위험선호/회피로 명확히 매핑되지 않아 `macro_score`에는 의도적으로 넣지 않음(설계 결정,
      TODO 아님) — `cross_asset_stress()`의 급변 크기 계산에만 반영.
- [ ] yfinance 폴백은 비공식 스크래핑이라 운영 신뢰도가 낮다(레이트리밋/스키마 변경 가능) — 정식
      운영 전 재점검 필요. **실거래 전환 시 항목별 필요 조치는 [[DECISION_LOG]] 2026-07-20
      "실거래 전환 시 데이터 소스 재검토 필요 항목 정리" 표 참고**(ZN/ES는 KIS CME 계열 유료
      구독 월 228.8불 또는 Databento GLBX.MDP3 월 $179로 대체 가능, MOVE는 KIS로 근본적으로
      해결 안 됨).
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
  - [x] (2026-07-19 검증 완료) **실제 Slack 메시지 발송 확인** — 실제 봇 토큰으로 채널
        (C0BJ7R4MZ9B)에 테스트 메시지 발송 후 사용자가 정상 수신 확인. 첫 시도에서 콘솔에 한글이
        깨져 보여 httpx json= 편의 파라미터가 charset을 안 붙이는 게 원인인 줄 알고
        `mahdi/notify.py`를 명시적 UTF-8 바이트+`charset=utf-8` 헤더로 고쳤으나, Slack
        `conversations.history`로 재확인해보니 **원래도 정상 전송되고 있었음**(깨져 보인 건 이
        Windows 콘솔(cp949)의 표시 문제였을 뿐) — 그래도 더 견고한 방식이라 되돌리지 않고 유지.
  - [ ] COCKPIT 체크박스를 브라우저에서 실제로 토글해 DB(`slack_alert_settings.enabled`)에 반영되고,
        관측 루프가 재시작 없이 그 값을 바로 따르는지 실운영 확인 필요(단위테스트로는 로직만 검증됨).
- [x] (2026-07-19 구현 완료) **로그 위생** — `logs/observation_loop.log`가 로테이션 없이 105MB까지
      누적된 문제(운영점검보고서 §5-5). `mahdi/main.py`에 `_configure_logging()`(신규) 추가 —
      `logging.handlers.RotatingFileHandler`(파일당 10MB, 최근 10개=최대 약 110MB)로 이 파일을
      Python이 직접 소유하도록 바꿈. `scripts/start_mahdi_premarket.bat`도 함께 수정해 stdout
      리다이렉트(`>> logs\observation_loop.log`)를 제거(안 걷어내면 Python이 회전시킨 파일을 이
      리다이렉트가 계속 원래 경로에 append해 로테이션이 무의미해짐) — 콘솔 창엔 여전히 실시간으로
      보이고, stderr만 별도 회전 없는 크래시 전용 로그(`logs\observation_loop_crash.log`)로 남김.
      `mahdi/logutil.py`(신규) `WarningThrottle`로 §3-1 NumericValueOutOfRange(한 사이클 안에서
      레그마다 반복 재발)와 WS 재연결 반복 실패 경고를 60초당 최초 1건만 로깅하도록 억제(억제된
      건수는 다음 로깅 때 요약으로 붙음)([[SESSION_LOG]] 2026-07-19 항목 참고).
  - [ ] **기존 104MB 로그 파일은 그대로 둠** — 다음 실제 관측 루프 실행 시 RotatingFileHandler가
        열자마자 maxBytes 초과를 감지해 즉시 `.1`로 회전시키고 새 빈 파일로 시작한다(Python
        표준 동작, 별도 조치 불필요) — 다만 그 첫 `.1` 백업 자체는 여전히 104MB이므로 디스크
        공간이 급하면 수동으로 지워도 됨.
  - [ ] 실제 관측 루프를 재시작해 콘솔 창에 로그가 여전히 실시간으로 보이는지, `logs/observation_loop.log`가
        실제로 10MB 근방에서 회전되는지 실운영 확인 필요(단위테스트는 tmp_path로 격리해 검증함).
- [x] (2026-07-19 구현+라이브 DB로 검증 완료) **COCKPIT "오늘의 점검 요약" 패널** — 운영점검보고서
      §1-B 장중 체크리스트 중 SQL로 자동화 가능한 항목(§5-6): 옵션체인/선물 데이터 결손(장중에만
      판단, §5-4 Slack 알림과 동일한 5분 기준), CBOT(zn_front) 승인 상태, series/symbol 화이트리스트
      위반(화석 데이터), 오늘 레짐 stability_flag 비율 — 5개를 COCKPIT 제목 바로 아래 배지로
      상시 노출. `mahdi/dashboard/data_source.py`에 `HealthCheck`/`get_health_summary()`(+ 5개
      개별 체크 함수) 신규, `mahdi/data/db.py`에 `expiry_liquidity_fossil_series()` 신규(화이트리스트
      밖 series 존재 여부 — `latest_expiry_liquidity()`는 이미 걸러서 반환하므로 "숨긴 것"과
      "없는 것"을 구분하려면 별도 조회 필요). 항목별로 독립적으로 DB에 접근해(하나 실패해도
      rollback 후 나머지는 계속 보여줌) 라이브 DB로 직접 조회해 정상 동작 확인함(장중 아님/CBOT
      미승인/화석 데이터 없음/오늘 레짐 데이터 없음 — 현재 시각 기준 전부 예상대로 표시)
      ([[SESSION_LOG]] 2026-07-19 항목 참고).
  - [ ] 실제 장중(평일 09:00~15:45)에 COCKPIT을 열어 옵션체인/선물 배지가 "정상"으로 정확히
        전환되는지, 결손을 실제로 유도했을 때(예: 폴링 중단) "결손" 경고로 바뀌는지 실측 확인 필요
        (지금까지는 장외시간 실측 + 단위테스트로만 검증됨).
- [x] (2026-07-19 구현+라이브 DB로 검증 완료) **20영업일 도달 카운트다운** — `feature_store` 축적
      현황을 "오늘의 점검 요약"(§5-6) 6번째 배지로 노출(§5-7). `_regime_fit_progress_check()`
      (`data_source.py`)가 `feature_store`에서 `count(*)`(총 행수)와
      `count(DISTINCT timestamp::date)`(실제 데이터 쌓인 날짜 수)를 함께 세어, 목표(8,000행/20영업일,
      `scripts/fit_regime_engine.py`의 `DEFAULT_MIN_SAMPLES`와 맞춤) 대비 진행률 + 하루 평균
      누적 속도 기반 잔여 영업일 추정치를 함께 보여줌. 론치일(07-05)부터 달력으로 계산하지 않고
      "실제로 데이터가 쌓인 날짜 수"를 직접 세는 방식을 택함 — 스케줄러가 쉬거나 실패한 날이
      있어도 자동으로 정확함(하드코딩된 론치일+영업일 계산보다 항상 실제 축적 상태를 정확히
      반영). 라이브 DB로 확인 결과 현재 1,764/8,000행(5/20영업일), 하루 평균 353행 기준 약
      18영업일 남음으로 표시됨 — 5영업일인 이유는 feature_store 실적재가 론치일(07-05)이 아니라
      `RegimeStateMachine` 실배선일(2026-07-10)부터 시작됐기 때문으로 추정(달력 계산이 아니라
      실측 카운트를 쓴 설계가 이 차이를 자동으로 반영함)([[SESSION_LOG]] 2026-07-19 항목 참고).
  - [ ] 목표(8,000행) 도달 시 실제로 "ok" 배지+`scripts/fit_regime_engine.py` 실행 안내가 뜨는지,
        그 시점에 실제로 그 스크립트를 실행해 `data/models/regime_engine.pkl`이 생성되고
        `RegimeStateMachine`이 predict() 모드로 전환되는지까지 이어서 확인 필요(대략 8월 초 예상).
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
- [x] 정규장 시간 중 `market_raw_1m`/`regime_state`에 실제 1분봉이 쌓이는지 확인 — 2026-07-09 DB 쿼리로 확인. 단, `option_analysis_1m`은 정규장 405분 중 203분(50%)치가 REST 500 대량 유실로 통째로 비어 있었음([[SESSION_LOG]], [[DECISION_LOG]] 참고) — 원인으로 추정되는 레이트리밋 미대응을 2026-07-09 레이트리미터+재시도로 수정.
  - [x] (2026-07-20 재확인 완료) "사이클 전체 유실"(위 203분 문제)은 재발 안 함 — 대신 지금까지
        계측된 적 없던 **레그 단위 결손**(콜은 거의 항상 성공, 풋만 계속 500)을 오늘 로그·DB로
        새로 발견함(콜 18~19건 vs 풋 3건/8분, 5개 행사가 전부 동일 경향) — 공유 `_RateLimiter`가
        행사가마다 콜→풋 순서로 호출하는데 2건/초(0.5초 간격)로도 KIS 모의투자 실제 한도(1건/초로
        재추정)를 넘겨 매 쌍의 두 번째 호출만 걸리는 패턴이었다. `DEFAULT_MIN_REQUEST_INTERVAL_SECONDS`를
        1.0초로 상향 + 적응형 백오프(EGW00201 감지 시 자동 확대/서서히 복귀) 추가로 수정,
        재시작 직후 실측으로 콜:풋 비율이 18:3(~17%) → 120:75(~63%)로 정상화됨을 확인
        ([[SESSION_LOG]] 2026-07-20 항목, [[DECISION_LOG]] 참고). **다음 확인 필요**: 정규장
        하루 운영 후에도 이 비율이 유지되는지, 적응형 백오프가 불필요하게 자주(레이트리밋이
        아닌 다른 500 때문에) 발동하지 않는지 DB/로그로 재확인.
- [ ] **2026-07-09 2차**: 위 대량 유실 수정 후에도 남아있던 5분 간격 잔여 유실(09:03/09:08/09:13/09:18 패턴, 405분 중 4분)을 사용자가 Gamma Wall에서 발견 — `poll_option_chain`/`poll_expiry_liquidity`/`poll_investor_flow`를 절대시각 고정 틱 스케줄링으로 전환 + `poll_expiry_liquidity` 시작 오프셋(30초) 추가로 수정 완료([[SESSION_LOG]], [[DECISION_LOG]] 참고). **다음 확인 필요**: 정규장 하루 운영 후 이 5분 간격 패턴이 실제로 사라졌는지 DB로 재확인.
- [ ] 정규장 시간 중 `logs/observation_loop.log`에 에러 없이 insert가 찍히는지 확인
- [ ] COCKPIT의 `st.rerun()` 10초 폴링이 브라우저에서 실제로 갱신되는지, 장시간(하루 종일) 방치 시 메모리/연결 누수 없는지 확인
- [x] `mahdi/dashboard/` 하위 모듈 수정 후 COCKPIT을 재시작해야 반영된다는 사실을 실제로 겪고 확인함
      (2026-07-06, [[DECISION_LOG]] 참고) — 앞으로 대시보드 코드를 고치는 세션에서는 체크리스트에 "COCKPIT
      재시작" 항목을 빠뜨리지 않을 것
- [ ] KIS 토큰 발급 레이트리밋(분당 1회 추정) 실운영 중 재현 여부 관찰 — 오늘 테스트 중 반복 호출로 403 재현됨

## 2026-07-20 점검·고도화 — 다음 확인 필요 (코드 변경 아님, 실운영 확인 대상)

- [ ] **사용자 확인 필요**: 사용자가 실전 선물옵션계좌(계좌번호 44833081, 근거계좌 -01/온라인
      개설 -03)를 신규 개설함 — 현재 `.env`에 설정된 KIS 모의투자 계좌(60045705,
      `KIS_ENV=vps`)와는 계좌번호 체계가 다른 별개 계좌로 보인다(모의투자 계좌는 보통 실전
      계좌와 독립적으로 발급됨). 이 신규 계좌가 (1) CBOT(해외선물옵션 SUB거래소) 미승인 문제를
      해소하는 것과 관계가 있는지, (2) 마흐디가 앞으로 이 계좌를 쓰도록 `.env`를 바꿔야 하는지
      (모의→실전 전환은 매우 신중해야 할 결정 — Phase 2 주문 실행 로직은 아직 미구현이라 지금
      당장은 데이터 수집에만 영향), (3) 아니면 완전히 별개 용도(예: 다른 프로젝트)인지 사용자
      확인 필요. `.env`는 이번 세션에서 건드리지 않았음([[SESSION_LOG]] 2026-07-20 3차 항목 참고).

- [x] (2026-07-20 3차 세션에서 CLI로 확인 완료) COCKPIT "오늘의 점검 요약"에 신규 추가한
      "옵션체인 콜/풋 균형" 배지가 정규장(09:00~15:45)에 실제로 ok/warning을 정확히 오가는지 —
      정규장 11시대에 `get_health_summary()`를 직접 호출해 7개 배지 전부(콜/풋 균형 포함)
      정상 표시 확인. **단, 이 확인 과정에서 별도의 심각한 버그(`_freshness_check`의
      naive/aware datetime TypeError로 헬스체크 전체가 다운돼 있었음)를 발견·수정함
      ([[SESSION_LOG]] 2026-07-20 3차 항목, [[DECISION_LOG]] 참고).**
  - [x] (2026-07-20 완료) 버그 수정 후 COCKPIT(Streamlit)만 별도 재시작(관측 루프는 무중단
        유지) → 사용자가 브라우저 스크린샷으로 7개 배지 정상 렌더링 확인(옵션체인/선물 "95초
        전 갱신", 콜/풋 균형 "콜 45건/풋 90건", CBOT "미승인" 등 기대한 그대로).
- [ ] `scripts/start_mahdi_premarket.bat`의 cockpit.log 회전(10MB 임계값, 기동 시점 1회 체크)이
      실제로 트리거되는 날이 오면(현재 3.7MB대) `.1` 파일이 정상 생성되는지 확인.
- [ ] `mahdi/main.py`의 `_log_startup_gap_since_last_run()`(직전 정상 기동 경과 로그)과
      `scripts/log_marketclose_stop.py`(직전 정상 종료 경과 로그)가 내일(2026-07-21) 07:30/15:45
      정규 스케줄 실행에서도 정상적으로 마커를 갱신하는지 확인 — 오늘은 수동 재시작으로만 검증함.
- [ ] `scripts/start_mahdi_premarket.bat`/`stop_mahdi_marketclose.bat` 실행 시 콘솔에 한글 REM
      주석 일부가 깨진 명령어처럼 보이는 현상을 이번 세션 도구 환경에서 재현함(이 저장소의
      수정 전 원본 배치파일도 동일 환경에서 동일하게 재현되는 것을 별도로 확인해, 이번 수정으로
      새로 생긴 문제는 아닌 것으로 판단) — Docker/COCKPIT/관측루프 실제 기동에는 영향 없음을
      확인했지만, 정식 Windows 작업 스케줄러 실행 로그에서도 부작용이 전혀 없는지는 아직
      확인 안 됨([[DECISION_LOG]] 2026-07-20 항목 참고).
- [ ] `mahdi/broker/rest_client.py`의 적응형 `_RateLimiter`(EGW00201 감지 시 간격 1.5배 확대,
      최대 4배, 연속성공 20회마다 서서히 복귀)가 실제 장중 트래픽에서 의도대로 동작하는지 —
      백오프가 걸렸다가 정상적으로 복귀하는 사례를 로그로 한 번 이상 확인할 것.

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
