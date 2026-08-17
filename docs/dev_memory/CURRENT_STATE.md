# CURRENT_STATE — 마흐디(options) 현재 개발 상태

_최종 갱신: **2026-08-17** (Phase 진단 + Phase 2 착수). 아래 절은 **날짜가 최신인 것이
우선한다** — 맨 위 절이 그 아래 모든 절보다 우선한다._

---

## [MW0601] 2026-08-17 갱신 — **우리는 Phase 1에 있지 않다. 이미 Phase 2 안에 있다**

> v6 PART 21 로드맵 기준으로 "현재 Phase 1"이라 읽어 왔으나, 코드 전수 확인 결과 **판단 층은
> 이미 Phase 2이고 실행 층만 미배선**이었다. 로드맵이 선형이라 진행도 선형일 것이라 읽은 것이
> 착오의 형태다. 테스트 1,500개 전부 통과(15.5초) 상태에서 조사했다.

### Phase 1 — 완료로 본다. 남은 셋은 Phase 1의 잔여가 아니다

| 항목 | 상태 |
|---|---|
| KIS 연동(토큰·WS·구독 롤링) | 완성, 실가동 |
| TimescaleDB 스키마 | 완성(마이그레이션 30개) |
| Regime Engine v1 (GaussianHMM 8-state) | 완성 + 오프라인 fit 배치 |
| COCKPIT v1 | 완성(요구 4패널 + 추가 5패널) |
| 피처 6종 | 6/6 구현, **VWAP·VAP만 라이브 미배선** |
| 피처 사전 문서(4요소) | 별도 md 없음 — **docstring 규약으로 내재화**(실질 충족) |
| 장전 매크로 나침반 | 원시 데이터·`cross_asset_stress`는 배선, §8 스코어는 프록시 |
| 주문 상태머신 모의계좌 검증 | 로직·테스트 완성, **실주문 0건** |

**VWAP/VAP 배선과 매크로 나침반 정식화를 Phase 1 잔여로 두지 않기로 했다.** 그 피처를 판단
입력으로 쓰는 시점에 배선해야 소비자가 있는 배선이 된다. 지금 하면 «만들어 두는 것과 도는
것은 다르다»의 역방향 — 도는데 아무도 안 보는 것 — 을 새로 만든다. 워치독(08-06 생성, 08-11
까지 미가동)과 같은 형태다.

### Phase 2 — 판단 층은 이미 라이브, 실행 층은 코드만 있다

| 항목 | 판정 |
|---|---|
| Signal Fusion | **라이브 배선**(60초 주기, `signal_decisions` 기록) |
| Risk Engine | **라이브 배선 — 단 «그림자 게이트»**(승인/거부를 기록만, 주문 없음) |
| Meta-Labeling | 결정론적 프록시. **Triple Barrier·Purged CV는 심볼 0건**(학습 데이터 대기 — 정상) |
| Execution Engine | 실구현, **`main.py`에 단 한 줄도 없음.** 유일 호출부는 백테스트 |
| 하이브리드 3모드 | 실구현, **미배선.** `main.py:3884`가 `exec_mode="ADVISORY"` 문자열 하드코딩 |
| 백테스트 + WFO·MC·DSR | 실구현, 오프라인 전용(정상) |

**`main.py`의 execution 임포트는 `account_tracker` 하나뿐이다.** `order_manager.submit()` ·
`forced_flat.build_forced_flat_orders()` · `evaluate_exit_stack()` 전부 프로덕션 호출부 0건.

### ⚠ 배선하는 순간 터지는 결함 2건 — 지금은 미배선이라 무해하다

1. **실행 경로에서 14:50 컷오프가 조용히 빠진다.** `execution/engine.py:78-80`이
   `risk_engine.evaluate_entry()`에 `now=`·`market_halted=`를 **안 넘긴다.** 기본값이
   `now=None`이고 그 뜻은 "시각 게이트를 건너뛴다"이다. `main.py:3863-3866`이 정확히 이
   위험을 예언해 뒀다 — *"이 인자가 비어 있으면 Phase 2에서 실행 엔진이 같은 호출을 복사해
   갈 때 시각 게이트가 조용히 빠진다."* **복사해 간 쪽이 이미 그 상태다.**
2. **청산 타임스톱이 실측 레짐 71.7%에서 무효.** `VOL_COMPRESSION` 행이 `exit_rules`에
   미정의다. 08-17에 조용한 False를 시끄러운 경고로 바꾸는 데까지는 했으나(`resolve_exit_params`),
   **값을 정하는 것은 사람의 일이라 아직 열려 있다.**

### 빠져 있던 계층 — §11.5

§11.4의 출력은 전략 **이름**이고 Execution의 입력은 **단축상품번호**인데 그 사이가 코드에도
스펙에도 없었다. 08-17에 스펙(§11.5)을 신설했고, 이번에 코드를 만들었다. 라이브가 미배선이라
아무도 이 빈칸을 밟지 않았을 뿐이다 — 백테스트는 `symbol="BACKTEST"` 자리표시자로 메우고 있었다.

---

## [MW0601] 2026-08-17 — 같은 날 구현분. **08-18 07:30 기동부터 실린다**

> 상세 결정 근거는 `DECISION_LOG.md` 2026-08-17. 예측치는 `hypotheses.yaml` `2026-08-17-p1~p4`.
> 테스트 **1,563개 전부 통과**(1,500 → +63).

마이그레이션 031은 **수동 적용이 필요 없다** — `start_mahdi_premarket.bat`이 매일 07:30 기동마다
`db/migrations/*.sql` 전체를 재적용한다(2026-07-21 신설, 전 파일 멱등). 08-17 라이브 DB 확인 완료.

| # | 무엇 | 되돌리면 깨지는 테스트 |
|---|---|---|
| 1 | `mahdi/fusion/instrument_selection.py` 신설 — §11.5 선택기(순수 함수) | `test_fusion_instrument_selection.py` 26건 |
| 2 | 마이그레이션 **031** `signal_decisions.selected_instruments` | `test_data_db.py` 2건 |
| 3 | 체인 스냅샷에 `delta`/`volume`/`spread_state` 합류 | `test_data_db.py::test_latest_option_chain_maps_rows_to_dicts` |
| 4 | **ExecutionEngine 시각·거래정지 게이트 복구**(P0급) | `test_execution_engine.py` 3건 |
| 5 | 백테스트 레짐을 실측으로(`exit_rules_regime`) | `test_backtest_data_adapter.py` 4건 |
| 6 | `hybrid_mode` 설정 읽기 + 배선 전 ADVISORY 클램프 | `test_execution_hybrid_mode.py` 5건 |
| 7 | ExecutionEngine 그림자 배선 | `test_main.py` 3건 |
| 8 | COCKPIT "선택 종목" 카드 | `test_dashboard_decision_panel.py` 5건 |
| 9 | 일일 리포트 `db.decisions.selected_instruments` 절 | `test_ops_selected_instruments_metric.py` 11건 |

### 새로 생긴 규약 — 기록에서 세 상태를 가른다

`signal_decisions.selected_instruments`가

- **NULL** → 선택기가 그 사이클에 안 돌았다(배선 이전 행).
- `{"candidates": [], "reason": "no_entry_strategy"}` → 돌릴 것이 없었다(팔레트 관망/방어 레짐).
- `{"candidates": [], "reason": "no_liquid_instrument" 등}` → 돌렸는데 못 골랐다.

**셋이 뭉개지면 이 컬럼은 쓸모가 없다**(규약 C). COCKPIT 카드도 같은 세 상태를 구분해 표시한다.

### 켜지 않은 것 — 유동성 임계는 **전부 비어 있다**

`strategy_params.yaml`의 `instrument_selection.liquidity`는 `min_oi: null` ·
`max_spread_state: null` · `require_traded_today: false` 다. **그 상태가 현재의 설계다** —
모의투자 실측 분포(`selected_instruments`의 후보에 실린 `oi`/`volume`/`spread_state`)를 본
뒤에 채운다. 며칠치가 쌓이기 전에 값을 넣지 말 것.

### ⚠ CONFIRM 승격의 하드 전제 — 옵션 가격이 수집되지 않는다

`option_analysis_1m`에 가격 컬럼이 **없다.** 지정가 진입 계획을 만들 수 없다는 뜻이고, 지금은
ADVISORY라 `build_entry_plan()`이 아예 안 불려 무해하다. 08-18 로그의
`KIS 옵션시세 output1 필드 목록` INFO 한 줄로 필드명을 확정한 뒤 컬럼을 만든다 —
**추측 금지**(`KIS_RAW_FIELD_RANGES.md` 체크리스트).

### 아직 중립 상수인 것 — 사이징 입력

`main.py`의 `target_vol=0.01, realized_vol=0.01, liquidity_score=1.0,
portfolio_capacity_remaining_pct=1.0`은 전부 하드코딩이다. Kelly 사이징의 **변동성 타게팅·유동성
축이 라이브에서 사실상 무력**이라는 뜻이다. 다음 작업이고, 한 번에 하나씩 켠다.

---

## 2026-08-12 **밤** 갱신 — 고도화 5건. 재연결 폭주의 진짜 원인을 찾았다

> 상세: `DECISION_LOG.md` 2026-08-12 밤 · 보고서 §5·§7-1.

**§7-1의 미해결 질문이 닫혔다.** 08-12에 WS가 31회 끊긴 것은 KIS도 네트워크도 아니라
**유지 풀이 죽은 클라이언트를 계속 쥐면서 생긴 자기지속 루프**였다. 진짜 네트워크 사건은
09:13:30 **한 번**뿐이다.

| 고도화 | 무엇을 했나 | 되돌리면 깨지는 테스트 |
|---|---|---|
| 1 | `ws_disconnect`(시간대별 단절) + `regime_vs_futures_bars`(봉 vs 레짐) + §11-2 | `test_ops_ws_reconnect_cost.py` |
| 2 | **`SubscriptionRetentionPool.rebind()` 신설** — 풀이 쥔 클라이언트 교체 | `test_data_subscription_manager.py` 3건 |
| 3 | 이미 Fix#8로 실렸다(저녁) | — |
| 4 | E 레버에 `전제레버` + 주장 지표에 `stale_pct` | `2026-08-12-eE-on-congested-hours` |
| 5 | 사후 평가 × `chain_input_source` + §14-4 | `test_ops_ws_reconnect_cost.py` |
| 6 | **구현 안 함(결정 대기)** — 재기동 범위 축소는 P3 그대로 | — |

### 왜 2가 P0급이었나

`rebind()`가 매니저의 `_ws`만 바꾸고 풀에는 `clear()`만 불렀다. 풀은 기동 시 한 번만
생성되므로 영원히 첫 클라이언트를 쥔다 → `free`를 죽은 클라 기준으로 계산하고
(`41 − 39 = 2`) → `ensure_free()`가 **죽은 커넥션에 unsubscribe** → 예외 → 재연결 →
다음 ATM 롤에서 반복.

**「무엇을 비울 것인가」는 07-19와 08-07이 두 번 검토했는데 「누가 그것을 쥐고 있는가」는
아무도 안 물었다.**

### 08-13에 새로 뜨는 절

`§11-2 WS 재연결`(단절 시간대 분포 + 재연결 1회당 잃은 분) · `§14-4 사후 평가 × 체인 입력 출처`.
둘 다 **임계를 안 건다** — 정상 분포를 모른다.

---

## 2026-08-12 **저녁** 갱신 — 감시자가 자기 일을 한 순간에 스스로를 꺼 버렸다 (Fix 8종)

> 상세: `docs/동작점검/2026-08-12_마흐디_운영점검보고서.md` / `DECISION_LOG.md` 2026-08-12 저녁
> **08-13 07:30 기동부터 실린다.** 예측치는 `hypotheses.yaml` `2026-08-12-fix1~fix8`.

| Fix | 무엇을 고쳤나 | 되돌리면 깨지는 테스트 |
|---|---|---|
| #1 P0 | 워치독 `_restart()`에서 `capture_output` 제거(→ `DEVNULL`) | `test_watchdog_observation_loop.py` 3건 |
| #2 P0 | 관측 루프 진입부 `get_quote`를 `httpx.HTTPError`에서 격리 | `test_main.py::test_a_kis_500_on_the_opening_quote_*` |
| #3 P0 | 봉 완성 핸들러에서 **레짐을 WS 재롤링 앞으로** | `test_main.py::test_regime_is_written_even_when_the_reroll_hits_a_dead_socket` |
| #4 P1 | 조기 포기를 2단계로(비우선 북 → 전부) | `test_main.py::test_timeout_abort_drops_the_weekly_book_before_the_monthly` |
| #5 P1 | 우선순위 위반을 「먼슬리가 **먼저** 잘렸다」로 재정의 | `test_ops_log_metrics_contract.py::test_priority_violation_label_*` |
| #6 P1 | §0 레버 표 + **규약 H**(`전제레버` → 「미실행」) | `test_ops_levers.py` 9건 |
| #7 P2 | 레짐 세션 창 복원 — **레버 OFF**(검증 전) | `test_regime_pipeline.py::test_session_window_restore_*` |
| #8 P2 | 워치독 판정 기록 + COCKPIT 배지 + §11-1 | `test_liveness.py` 8건 · `test_ops_watchdog_metrics.py` |

### 새로 생긴 것

- `mahdi/ops/levers.py` — 그날 어떤 레버가 켜져 있었는가 + git HEAD.
- `mahdi/ops/watchdog_metrics.py` — `logs/watchdog.log`의 **침묵**을 잰다(`silence_over_cadence_ratio`).
- `logs/.watchdog_last_check.json` — 워치독이 **판정할 때마다**(IDLE 포함) 갱신. COCKPIT 배지
  「워치독 판정 신선도」가 3분 임계로 읽는다. **실시간 소비자는 §11-1이 아니라 이 배지다.**
- 컷 로그 세 줄에 `· 우선순위위반=예|아니오`가 붙었다(파서 계약 갱신 완료).

### 켜지 않은 레버 (§0 표가 매일 인쇄한다)

`use_effective_member_count` · `reentry_cooldown_minutes` · `SIGNAL_FUSION_PHASE_OFFSET_SECONDS`
· `OPTION_CHAIN_SLOW_SERIES_CONGESTED_HOURS` · `OPTION_CHAIN_READ_TIMEOUT_SECONDS`
· `REGIME_RESTORE_SESSION_WINDOW` — **전부 OFF.**

### ⚠ 이 PC 밖으로 안 따라가는 것

`Mahdi-Watchdog`의 `ExecutionTimeLimit = PT10M`은 **OS에 있다.** 저장소를 pull해도 안 따라오므로
새 PC에서는 아래 「워치독 등록」 절의 절차를 다시 밟아야 Fix#1의 백스톱이 성립한다.

---

## 2026-08-07 **저녁** 갱신 — 시장 시간을 몰라서 나흘을 장애로 신고했다

> 상세: `docs/동작점검/2026-08-07_마흐디_운영점검보고서.md` / `DECISION_LOG.md` 2026-08-07 저녁
> 전일 완결(495분) 분석. 아래 「낮 갱신」 절과 **함께 08-10 07:30 기동부터 실린다.**

### ⚠ 반드시 알고 있어야 할 것 — 하루는 **세 구간**이고, 그 경계가 시장마다 다르다

    07:30~09:00   지수 없음(장전, 2026-08-05 `9ffcb9c`)        선물 WS 있음
    09:00~15:20   지수 실시간                                  선물 WS 있음
    15:20~15:45   지수 없음(현물 마감, 2026-08-07 Fix#1·#2)    선물 WS 15:35까지

**현물(유가증권시장)은 15:20~15:30이 장 마감 동시호가**이고 그 뒤로는 종가에 고정된다.
파생은 15:45까지 거래된다. 두 경계를 합치면 15:20~15:35의 25분이 어느 쪽으로든 틀린다 —
`session.EQUITY_CONTINUOUS_TRADING_END`(15:20)와 `CLOSING_AUCTION_START`(15:35)는 **다른 시장**이다.

08-04~08-07 나흘 내내 15:21~15:34가 「지수 정지」로 잡혀 원인 규명 대기 목록에 있었다.
매일 정확히 9분이라는 규칙성이 답이었다. 재집계 결과 `index_frozen_max_run` 9 → **1**.

**그 25분 동안 `options_flow`는 구조적 미가용이 된다**(사용자 결정 (c)). 스팟 적재를 끊으므로
장전 처리와 완전히 대칭이다. 가용률이 81.8% → 약 77%로 내려가는 것은 **대가이지 회귀가 아니다** —
그 25분은 원래 죽은 스팟으로 GEX를 계산하고 있었다. 진입 컷오프(14:50)가 앞서므로 신규 진입
영향은 없다.

### 「덮어쓴 분」은 결손보다 나쁘다

15:18에 DB 0행인데 로그는 완주 — 그 사이클이 15:17:59.99x에 깨어 `poll_time`이 15:17로
내려깎였고 **직전 분을 UPSERT로 덮어썼다.** 15:17의 데이터는 실제로 15:18에 수집된 값이다.
행 수가 정상이라 어떤 지표도 못 잡았다.

낮에 넣은 Fix#3(2초 경계 스냅)이 원인을, 저녁 Fix#3(사이클 로그 `분=HH:MM` + 중복 카운터)이
회귀 감시를 맡는다. **`count`와 `labelled`를 함께 읽을 것** — 라벨 0이면 count 0은 증명이 아니다.

### 규약 F 신설 — 주장 지표를 절대 건수로 세우지 않는다

08-07 하루에 같은 형태의 예측 오류가 **세 번** 났다(p4/e1/p2 — 창 길이·이동거리·북 수를
각각 빠뜨렸다). 셋 다 fix는 맞았는데 「반증」으로 찍혔다. `hypotheses.violates_normalized_claim_rule()`
+ 파일 전체 검사 테스트가 이제 그것을 막는다 — **도입 당일 살아 있는 예측 셋을 잡았다.**

### 새로 생긴 관측 축

- **REJECT 대조군**(고도화#C) — 시간대를 맞춘 뒤에도 08-07은 전 지평에서 거른 판단이 더 잘
  맞혔다(30m −11.3pt). **하루치로 결론 내지 않는다.** 적재하지 않고 읽을 때 계산한다.
- **부호 일치율 전일 대비 변화폭**(고도화#B) — 08-07 −61.5pt(74.6% → 13.1%).
  낮에 제기한 "부호 규약이 뒤집혔을 가능성"은 **기각**됐다(규약 버그라면 매일 낮아야 한다).

### 결정: 고치지 않는 것

- **EGW00201 1건**(15:02:35 USDCNH) — 그 1건이 백오프를 1.50배로 올렸다. 자동 판정은 반증
  2건으로 셌지만 실체는 하나다. 폴러 위상 재조정은 07-08에 203분을 잃은 구조라 관측만 유지한다.
- **KIS 지연 레버** — 조건은 더 강해졌지만(p95 5구간 이틀 연속) 08-10에는 네 묶음의 fix가
  처음 실린다. **변수를 하나만 바꾸는 원칙**상 08-11에 재판단.

---

## 2026-08-07 **낮** 갱신 — **계측 둘이 자기 주석과 다른 것을 재고 있었다**

> 상세: `docs/동작점검/2026-08-07_마흐디_운영점검보고서.md` / `DECISION_LOG.md` 2026-08-07
> 조사 범위는 **반나절(07:30~12:20, 287분)** 이다.

### ⚠ 반드시 알고 있어야 할 것 — GEX 시계열은 08-10에 단절된다

08-07까지 **장중 판단의 78%(157/202분)가 서로 다른 두 개 이상의 ATM 창을 섞어 GEX를 냈다.**
5분 신선도 창 안에 ATM 롤로 생긴 다른 행사가 창이 겹쳐 쌓이고, `DISTINCT ON`이 창 밖으로
빠져 더 이상 폴링되지 않는 행사가의 마지막 값을 창 만료까지 붙들기 때문이다.

Fix#1(`db._restrict_to_latest_cycle_window`)이 08-10부터 그것을 걷어낸다. 그래서:

- **|GEX|가 줄어드는 것은 성공이지 회귀가 아니다.** 08-07 이전 시계열과 직접 비교하지 말 것.
- `short_gamma_requires_not_met` 건수(08-07 49건)와 §15 핀 행사가/집중도가 바뀔 수 있고,
  **그 변화는 이 fix가 만든 것**이다.
- 회귀 감시는 `db.signal_reach.chain_leg_over_design_minutes`(불변식 0, 08-07 기준선 **210분**).

### ATM 롤에 대한 이해가 08-07에 바뀌었다

배지 임계 20회를 이틀 연속 4배 가까이 넘겼지만(08-05 77 / 08-07 76), **전수 검산 결과 전부
정당한 롤**이었다(히스테리시스 임계 1.875p를 실제로 넘었고 그날 지수가 40p 움직였다).

**롤 횟수는 시장 변동성의 함수라 통제 대상이 아니다.** 통제 대상은 그 롤이 구독을 실제로
끊었는가이며, 그것이 마이그레이션 028의 `atm_roll_dropped_subs_today`다. 배지 판정 축도
그쪽으로 옮겼다 — **롤 76회인데 배지가 초록인 것이 목표 상태다.**

구독은 `SubscriptionRetentionPool`이 슬롯 남는 동안 유지한다(LRU 축출). WS 구독은 REST
페이서 예산을 안 쓰고 REST 폴링 대상은 `desired_strikes`에서 나오므로 **REST가 안 는다** —
이 전제가 깨지면(`rest.by_group.옵션체인` 급증) 즉시 되돌린다.

### 08-10(월)이 HMM 재학습 임계 도달일이다

`feature_store` 08-07 장중 7,709 / 8,000. 드라이런에서:

- ✅ 잠재상태 방문 **8/8개**(08-03의 "1~2개면 모델 구성 의심" 경고 해소)
- ⛔ **EM 수렴 실패** — `fit()`이 예외를 안 던지므로 그대로 저장하면 나쁜 모델이 조용히 올라간다.
  → `monitor_.converged`가 True가 아니면 **저장 거부**가 기본값이다(`--allow-nonconverged`로 우회).

**확신도 분모 전환(`use_effective_member_count`)은 재학습과 같은 날 켜지 않는다** — 확신도
변화를 둘 중 무엇이 만들었는지 못 가른다. 재학습 성공 확인 **다음 영업일**에 올린다.

### 새 마이그레이션

- `028_ws_status_atm_roll_cost.sql` — `ws_status.atm_roll_dropped_subs_today`(nullable).
  026과 같은 이유로 DEFAULT를 두지 않는다: 구 코드가 남긴 0은 "대가가 없었다"는 거짓말이 된다.
  **08-07 장중에 라이브 DB에 적용해 뒀다**(그날 저녁 자동 집계가 `ws_status` 절을 잃지 않도록).

### 미해결 이월

- **ReadTimeout 92건 / KIS `inquire-price` p95 3구간 이틀 연속 초과** — KIS 귀속. 사전 대응
  레버(`2026-08-04-p5`)는 발동 조건 성립했으나 **계속 내려 둔다**(먼슬리 두께가 회복됐다).
- **`flow_position ↔ options_flow` 부호 일치율 22.3%** — 무작위면 50% 근처여야 한다.
  고도화#5로 3영업일 추이가 보이게 됐다. **임계는 아직 정하지 않는다.**
- **장운영정보(H0UNMKO0) 누적 수신 0건** — 배지를 "미검증"으로 바꿨다(Fix#6). 다음 단계는
  KIS 문서로 정상일 기대 동작을 확인하는 것.
- **옵션 WS 1분봉이 얇다** — 21종목 구독인데 분당 평균 1.5종목만 봉이 생긴다.
  `orderflow_ofi_vpin`의 입력이다.

---

## 2026-08-11 **저녁** 갱신 — Fix 10종 + 고도화 6종. 재는 자가 틀린 날이었다

> 상세: `docs/동작점검/2026-08-11_마흐디_운영점검보고서.md`. 예측은 `hypotheses.yaml`
> `2026-08-11-*` 8건에 전부 등재. **08-12 07:30 기동부터 실린다.**

### ⚠ 규약 G 신설 — 시장 상태 의존 지표에 무조건부 하한을 걸지 않는다

08-11에 레짐 엔진이 25영업일 만에 완벽히 돌았는데(09:14 predict, 3종 방문, 전이 2회) 검증
기준 **둘이 반증을 찍었다**: `regime_hmm 비영 분 > 0`, `dead_axis_mean < 1.02`.

정체는 `signal_layer._TREND_DIRECTION`이 **TREND_UP/DOWN_STRONG 두 상태에만** 방향을 준다는
것이다(v6 §7). 그날 방문한 셋은 전부 방향이 없어 `regime_hmm`이 419분 전량 0점이었다 —
**설계대로.** 그 기준은 *"엔진이 도는가"* 와 *"엔진이 추세를 봤는가"* 를 섞고 있었다.

규약 F(건수는 구조 변수에 비례한다)와 **같은 병의 다른 얼굴**이다.
`hypotheses.violates_market_state_rule()`이 막고, **도입 당일 `2026-08-06-e2`의 살아 있는
예측을 잡았다**. 전제 조건은 지표로 만들었다 — `db.regime.trend_minutes`가 0이면 판정 불가다.

### 22분 전멸의 정체 — 우리 타임아웃이 KIS 지연을 100% 실패로 바꿨다

15:01~15:22 적재 0행. 느린 호출 줄의 HTTP 성분이 **4.03~4.06초에 못 박혀** 있었다(= read
타임아웃 4.0초에 닿아 끊긴 시각). 페이서 배율은 내내 1.00배 — 우리 쪽 압력이 아니다.

    레그당 = 페이서 1초 + 타임아웃 4초 = 5초  →  50초 예산에 10레그  →  전부 실패 → 0행

**실패가 성공과 같은 예산을 먹으므로 사이클이 스스로 못 빠져나온다.** 두 층으로 막았다:

- **Fix#1** 연속 타임아웃 3회 → 조기 포기(빠른 감지, 전멸 패턴)
- **고도화 A** 누적 실패 6건 → 조기 포기(누적 감지, 섞여서 죽는 패턴 — 08-11 14시대가 그것)

두 원인과 예산 초과를 **서로 다른 로그 줄**로 낸다. 08-11에는 셋이 한 줄이었고, 그래서
22분 전멸과 "조금 잘린 분"이 같은 지표로 보고됐다.

### 계측 셋이 자기가 재는 것을 정확히 몰랐다

- **`overrun` 감사가 매일 거짓 ⚠를 냈다**(Fix#7). 파서는 옳았고 **느슨 토큰(`"스케줄이"`)이
  틀렸다** — 여섯 폴러가 같은 문장을 쓴다. 토큰을 좁히고 `overrun_by_poller`를 신설했다.
  08-11 만기유동성 밀림 1건은 종전에 **오경보로만 존재했다.**
- **예산 컷이 어느 북에 닿았는지**를 안 남겼다(Fix#2). 08-06이 그 값을 손으로 세어
  고도화#1의 방향을 정했는데 지표로는 없었다. 이제 `priority_cut_minutes` 불변식이다.
- **판단이 「그 분」 체인을 봤는지**를 안 남겼다(고도화 B, 마이그레이션 029).
  08-11 실측 홀수분 28초 / 짝수분 70초 — 하루의 절반이 직전 분 GEX였다.

### 배치 규약이 늘었다 (Fix#8 / 문서)

`hypotheses.yaml` 확정 대기 23건을 08-11 실측 근거로 전부 닫았다(확인 18 / 반증 1 /
inconclusive 4). **경과 항목 0건.** 90일 초과 pending은 이제 `stale` 플래그가 붙고 리포트가
경과일 내림차순으로 낸다 — 목록이 길어지면 진짜 반증이 소음에 묻힌다.

### 내려 둔 레버 셋 (조건 성립해도 오늘 안 켬)

`rest_client.OPTION_CHAIN_READ_TIMEOUT_SECONDS`(Fix#3) /
`main.SIGNAL_FUSION_PHASE_OFFSET_SECONDS`(Fix#10) /
`strategy_gates.reentry_cooldown_minutes`(고도화 D). 켤 조건과 예측치는 각 상수 위 주석에 있다.

---

## 2026-08-11 갱신 — 먼슬리를 `max(만기)`로 고르던 규칙이 공휴일에 뒤집혔다

> 상세: `docs/동작점검/2026-08-11_마흐디_운영점검보고서.md`. **개장 전(08:35)에 고치고 재시동했다.**

### ⚠ 먼슬리 북 선택은 이제 `options_intel.monthly_expiry()` 하나가 정한다

종전 규칙은 `max(expiry)`였고 근거는 *"위클리는 늘 먼슬리보다 가깝다"* 였다. **08-11에 깨졌다** —
08-13(목)이 8월물 만기인데 08-15 광복절이 토요일이라 **08-17(월)이 대체공휴일**이 되면서
위클리(월) 만기가 **08-18로 먼슬리 뒤로** 밀렸다.

`signal_book_legs()`·`monthly_atm_iv()` 두 docstring이 **이 위험을 미리 적어 뒀고 완화까지
달아 뒀다** — *"뒤집혀도 그때는 먼슬리가 만기 당일이라 GEX가 0이 된다."* 그 완화는 **만기
당일에만** 성립하고, 실제 사고는 **만기 이틀 전**에 났다(위클리가 건너뛴 것이지 먼슬리가
가까워진 게 아니다). **위험을 알고 적어 둔 완화가 실제 발생 형태를 못 덮은 사례다.**

피해는 R6이 없앤 구형파의 재발이었다. 위클리는 격분에만 조회되므로 `max()`가 분 단위로 교대했다:

    홀수분  max = 08-13 (먼슬리 단독)      ATM IV 0.7910
    짝수분  max = 08-18 (위클리가 섞임)    ATM IV 0.6229   격차 0.168

08-10에 R6이 없앤 구형파가 0.210이었으니 **그 80%가 다른 원인으로 같은 자리에 돌아와 있었고,
하필 HMM이 처음 라이브로 도는 날이었다.** 수정 후 같은 데이터에서 **격차 0.0000**.

- 규칙: **그 달 두 번째 목요일**인 후보가 정확히 하나면 그것. 아니면 `max()`로 폴백(종전 동작).
- **`series` 컬럼을 안 쓴 이유**: `option_analysis_1m`에 없고, 추가하면 과거 행이 NULL이라
  오프라인 재계산(`reconstruct_iv_chg`)이 라이브와 갈라진다 — 그 분기가 08-10 사고의 구조다.
  날짜 규칙은 과거 행에도 똑같이 적용된다.
- 판단(GEX/감마플립)과 레짐 피처가 **같은 함수**를 부른다. 규칙이 두 벌이면 한쪽만 고쳐진다.

### 워치독은 08-06부터 08-11까지 **한 번도 실행된 적이 없었다**

미등록이 원인의 전부가 아니었다. `scripts/watchdog_mahdi.bat`이 `chcp 65001` + UTF-8 한글 주석
조합으로 **cmd.exe의 배치 읽기 오프셋이 어긋나** 주석 중간부터 파싱됐다 — `REM` 접두사를 잃고
주석 조각이 명령으로 실행됐고, 그중 하나가 **주석 안 예시로 적혀 있던 `schtasks /Create`** 였다.
`cd /d`도 실패해 **호출자가 이미 프로젝트 루트일 때만** 동작했다.

- **배치파일 규약 2조 추가**: ① 비ASCII 금지(주석 포함) ② 주석 안에 실행 가능한 명령 금지.
  기존 규약(CRLF 필수)에 이어진다.
- `scripts/watchdog_mahdi_hidden.vbs`(신규) — 콘솔 창 숨김 런처. 작업 스케줄러의 "숨김"
  체크박스는 **목록에서만** 숨기지 창은 못 숨긴다. 워치독은 재기동 시 사용자 세션에 창을
  띄워야 하므로 "로그온 시에만 실행"이 필수이고, 그래서 창 숨김을 vbs가 맡는다.

**등록(PC별 1회, 관리자 PowerShell)** — 배치 주석에 두지 않는다(위 규약 ②):

```powershell
schtasks /Create /TN "Mahdi-Watchdog" /SC MINUTE /MO 1 /TR "wscript.exe //B //Nologo <프로젝트경로>\scripts\watchdog_mahdi_hidden.vbs" /RL HIGHEST /F
```

- **`/TR` 안에 따옴표를 넣지 말 것.** 첫 시도(08-11 08:39)가 `\"경로\"`로 감쌌다가 등록은
  "SUCCESS"인데 **동작은 깨졌다**: PowerShell 5.1은 `\"`를 자기 이스케이프로 안 보고 그대로
  네이티브 인자로 넘기는데, 경로 끝 `.vbs\"`에서 백슬래시가 따옴표를 이스케이프해 문자열이
  거기서 안 끝났다. 결과로 **`/RL HIGHEST /F`가 `/TR` 값 안으로 빨려 들어갔고**, 스위치로
  도달하지 못해 RunLevel이 `Limited`로 남았다. 저장된 값이 그대로 증거다:

      Argument: [" C:\...\watchdog_mahdi_hidden.vbs\ /RL HIGHEST /F]

  wscript는 그 뭉친 문자열을 상대경로로 받아 `C:\WINDOWS\system32\` 기준으로 찾다 실패하고
  **모달 오류 대화상자**를 띄웠다 — 작업이 `Running`에 멈춰 후속 실행이 전부 무시됐다.
  **"SUCCESS"는 등록이 됐다는 뜻이지 인자가 옳다는 뜻이 아니다.**
- 경로에 공백이 없으면 따옴표가 필요 없다. 공백이 있는 PC라면 `schtasks --%` 로
  **정지 파싱 토큰**을 쓰거나 `Register-ScheduledTask -Execute/-Argument`(인자를 따로 받아
  인용이 아예 없다)를 쓸 것.
- `//B //Nologo`는 wscript가 실패해도 **대화상자를 안 띄우게** 한다. 1분 주기 작업이 모달
  창을 띄우면 그 순간부터 워치독이 멈춘다(위가 정확히 그 사고다).

**등록 직후 반드시 저장된 값을 되읽어 확인할 것** — 등록 성공 메시지로는 판정하지 않는다:

```powershell
$t = Get-ScheduledTask -TaskName 'Mahdi-Watchdog'
$t.Actions[0].Execute; $t.Actions[0].Arguments; $t.Principal.RunLevel   # Highest 여야 한다
schtasks /Run /TN "Mahdi-Watchdog"                                       # 즉시 1회 실행
(Get-ScheduledTaskInfo -TaskName 'Mahdi-Watchdog').LastTaskResult        # 0 이어야 한다
```

최종 확인은 `logs/watchdog.log`다 — 매 `:x0`분에 `OK` 줄이 붙는다(정상은 10분에 한 번만 기록).

**2026-08-11 08:40 이 PC(MW0601) 등록·검증 완료** — `[2026-08-11 08:40:48] OK — 정상`.

**2026-08-12 추가 — `ExecutionTimeLimit`을 반드시 낮출 것(PC별 1회).** 기본값 `PT72H`는
막힌 워치독을 **3일간** 안 죽인다. 08-12에 재기동 호출이 상속된 파이프에 물려 5시간 31분
매달렸고, `MultipleInstances=IgnoreNew`라 그동안 매분 실행이 전부 무시됐다(§2-3).

```powershell
$t = Get-ScheduledTask -TaskName 'Mahdi-Watchdog'
$t.Settings.ExecutionTimeLimit = 'PT10M'
Set-ScheduledTask -TaskName 'Mahdi-Watchdog' -Settings $t.Settings
(Get-ScheduledTask -TaskName 'Mahdi-Watchdog').Settings.ExecutionTimeLimit   # PT10M 이어야 한다
```

- **`PT5M`이 아니라 `PT10M`이다.** 보고서 초안은 5분을 적었는데 그러면 재기동 타임아웃
  (`_RESTART_TIMEOUT_SECONDS = 300초`)과 **정확히 같아져** 정상적인 느린 기동을 OS가 중간에
  자른다. 백스톱은 그 두 배여야 한다.
- **`MultipleInstances`는 `IgnoreNew` 그대로 둔다.** 중복 기동이 더 위험하다 —
  막힘은 위 상한과 `capture_output` 제거(Fix#1)가 함께 막는다.
- 이 값은 저장소가 아니라 **OS에 있다.** 워치독을 새로 등록하는 PC마다 다시 해야 한다.

**2026-08-12 16:5x 이 PC(MW0601) `PT10M` 적용·되읽기 확인 완료.**

---

## 브랜치 / 환경
- 저장소: 2026-07-05 `git init`, 단일 `master` 브랜치
- Python: 3.12 (uv 관리 가상환경, `.python-version`)
- DB: TimescaleDB(`mahdi_timescaledb`) + Redis(`mahdi_redis`), `docker-compose.yml`
- KIS 계좌: 모의투자(VPS), `.env`에 앱키/시크릿/계좌번호 보관(gitignore, 커밋 안 됨)

---

## 2026-08-03 갱신 — **알려진 결함 1건과 그 수정**

> 상세: `docs/동작점검/2026-08-03_마흐디_운영점검보고서.md`

### ⚠ 반드시 알고 있어야 할 것 — 감마플립은 **전 이력에서 한 번도 산출된 적이 없었다**

`find_gamma_flip()`이 넉 달 내내 `None`만 반환했다. 원인 셋이 겹쳐 있었고 08-03에 전부 고쳤다:

1. **NaN 오염** — `iv=0`인 레그가 하나만 섞여도 `gex_at()`의 **합계 전체**가 NaN이 되고,
   NaN은 `values[i-1]*values[i] < 0`을 항상 False로 만들어 조용히 `None`으로 떨어졌다.
   → `usable_for_black_scholes()`로 계산 전 배제 + 산출 불가 시 WARNING.
2. **체인 스냅샷에 경계 없음** — `latest_option_chain()`에 시각·만기 조건이 없어 246레그 중
   오늘 수집분이 10개(만기 지난 것 156개, 최고령 4주)였다. GEX 부호까지 뒤집혔다.
   → 10분 신선도 창 + `expiry >= today` + `DISTINCT ON`에 `expiry` 추가.
3. **ATM 롤링 1회 실행** — `roll_to_spot()`이 WS 연결당 한 번만 호출돼 07:31 장전 호가로 잡힌
   행사가가 하루 종일 고정됐다(스팟 988에 행사가 1042~1052, 5.4% 외가격).
   → 선물 1분봉 완성 시마다 `_reroll_books_to_spot()`.

**파급**: 앙상블 멤버 `options_flow`(v6 §11.3 base_w 0.20)가 영구 미가용이었다 —
`signal_decisions` 전 이력에서 `available_member_count >= 3`인 행이 **0건**.
즉 판단은 "레짐(RANGE_BALANCED라 방향 0.0) + 외국인 순매수 부호" 둘로만 내려지고 있었다.

**Phase 2 진입 전 반드시 확인할 것**: 08-04 이후 `db.signal_reach`의 `member_count_max`가 3이 되고
`gamma_flip_pct`가 올라오는지. 안 오르면 행사가 창(ATM±2)이 좁아서이며, 그건 §4 "사용자 결정 대기 #1".

### 폴러 위상 — **08-03에 전면 재배치**(위 08-01 표는 낡았다)

위클리 월/목을 짝/홀 분으로 나눠 태워 **매 분 20레그로 평탄화**했다(총 REST 수요 불변).
그에 맞춰 나머지 폴러를 옵션체인 창 뒤로 옮겼다:

| 폴러 | 주기 | 위상 | 발사 분(mod 10) | 콜/사이클 |
|---|---|---|---|---|
| `poll_option_chain` | 60초 | 0초 | 매 분 | **20 (전 분 동일)** |
| `poll_expiry_liquidity` | 60초 | **30초** | 1, 3, 5 | 11 |
| `poll_macro_snapshot` | 300초 | **150초(2:30)** | 2, 7 | ≤7 |
| `poll_account_balance_cycle` | 300초 | **270초(4:30)** | 4, 9 | 1 |
| `poll_investor_flow` | 60초 | **50초** | 매 분 | 3 |

- **만기유동성·매크로·계좌잔고가 같은 30초 위상을 공유한다** — 발사 분 집합 {1,3,5}/{2,7}/{4,9}가
  서로소라 같은 분에 만나지 않는다. 모든 분이 동일 구조(0~20 체인 / 30~41 저빈도 / 50~53 수급).
- 분당 호출 수 14~36 → **23~34**(평균 26.8 불변).

### 새 관측 지표 / 마이그레이션

- **021** `ws_status.market_op_subscribed_at` — H0UNMKO0 **구독 성립** 시각.
  `last_message_at`(데이터 수신)에는 임계를 걸 수 없다(정상일에도 하루 0~2건, 08-03은 0건).
- **022** `signal_decisions`의 `gamma_flip`/`gex`/`chain_leg_count`/`chain_oldest_leg_age_seconds` —
  판단 시점의 체인 입력. 이게 있어야 "신호 도달률"을 사후 집계할 수 있다.
- `db_metrics.signal_reach()` → 자동 리포트 §14 + COCKPIT 배지(같은 함수·같은 임계 공유).
- `db_metrics.book_gamma_map()` → 리포트 §15. **북별로 GEX 부호가 반대일 수 있다**
  (08-03 먼슬리 −4.76bn vs 만기당일 +2.87bn) — 합산하면 §11.4 게이트 판정이 뒤집힌다.

### COCKPIT — 4주 만의 육안 확인이 배지 버그 2건을 잡았다

**교훈: 자동 리포트가 맞아도 배지는 틀릴 수 있다.** 08-03에 리포트는 먼슬리 커버리지 98.8%로
정확했는데 배지는 **120.7%**를 초록불로 띄우고 있었다(분자는 하루 전체, 분모는 09:00 이후 경과).
배지가 `< 95%`만 경고하도록 돼 있어 **고장난 지표가 정상으로 보였다.**

- `monthly_book_coverage()`는 이제 `elapsed_minutes`를 안 받으면 `observed_span_minutes()`로
  **분자와 같은 구간**을 분모로 쓴다. 100% 초과 시 `over_100` 플래그로 경고한다.
- `_freshness_check(..., continuous_trading_only=True)` — 종가 단일가(15:35~15:45)에는 연속
  체결이 없어 WS 봉이 안 만들어진다. 선물 배지가 **매 거래일 15:40~15:45에 오경보**를 냈다.
  이제 그 구간에서는 나이를 단일가 시작 시각 기준으로 잰다(선물 배지에만 적용 — 옵션체인은
  REST 폴링이라 단일가 구간에도 계속 들어온다).

**COCKPIT은 Streamlit이라 브라우저를 열어야 스크립트가 돈다** — 아무도 안 열면 `cockpit.log`가
기동 줄에서 멈춘다(08-03이 그랬다). 그리고 **떠 있는 프로세스는 코드 변경을 반영하지 않는다**.

### 판단 축 로깅 — 08-03까지 로거가 **3개뿐**이었다

`httpx` / `mahdi.main` / `mahdi.broker.rest_client`. `ws_client`는 로거 자체가 없었고
`engines`·`risk`·`fusion`·`features`는 전부 무음이었다 — 위 결함이 넉 달간 안 보인 이유다.
**전이(transition)에만 반응하는** 로거를 신설했다: 판단 형태 전이 / 피처 중립값 최초 탈출 /
레짐 전이 / WS 구독·해제·ACK / ATM 롤링.

### HMM 학습 — 드라이런에서 **비수렴 발견**(08-10 전에 잡았다)

`fit_regime_engine.py --dry-run`이 `fit()`은 통과하는데 6회 재시작 전부 비수렴(로그우도 −2.2e22).
원인은 `_series_zscore()`의 0-분산 가드가 `std <= 0`이라 부동소수 잡음(std≈1e-13)을 못 걸러
z가 1e12까지 폭발한 것. **상대 오차 판정 + ±10 클램프**로 수정 → 로그우도 +22,383, 잠재상태 8/8 방문.
`rv_ratio`는 여전히 분산 0이며 **08-04에 자연 해소** 예정(유효 종가 20일 → 21일).

---

## 2026-08-01 현재 스냅샷 (07-11 ~ 08-01 요약)

### 실행 모드
**ADVISORY 전용** — 판단은 매분 수행되고 `signal_decisions`/`risk_snapshots`에 기록되지만
**실주문 경로는 배선돼 있지 않다**. `ExecutionEngine`은 구현만 돼 있고 관측 루프에 연결되지 않았다.

### Phase 2 모듈 배선 상태

| 모듈 | 구현 | 라이브 배선 | 비고 |
|---|---|---|---|
| `mahdi/risk/engine.py` (Core Engine 7) | ✅ | ✅ 조건부 | 진입 후보가 있을 때만 `evaluate_entry()` — 07-31 기준 호출 0회 |
| `mahdi/risk/market_halt.py` (KRX CB 감지) | ✅ | ✅ | H0UNMKO0 구독 + 독립 하트비트(08-01) |
| `mahdi/risk/circuit_breaker.py` | ✅ | ✅ 계측만 | 일간 래치라 `evaluate()`는 여전히 안 부르고, 08-01부터 매 사이클 `evaluate_readonly()`(상태 불변)로 조건을 평가해 `risk_snapshots`에 남긴다 |
| `mahdi/fusion/*` (Signal Fusion) | ✅ | ✅ | `poll_signal_fusion_cycle`, ADVISORY |
| `mahdi/execution/*` (Execution Engine) | ✅ | ❌ | **재진입 방지 로직 부재** — 배선 전 선행 해결 필요 |
| `mahdi/execution/account_tracker.py` | ✅ | ✅ | `poll_account_balance_cycle`(300초) |
| `mahdi/backtest/*` | ✅ | ❌ | 오프라인 전용 |
| `mahdi/engines/regime.py` (HMM) | ✅ | ⚠️ **폴백 중** | `data/models/regime_engine.pkl` 부재 → `warmup_fallback()` 자기참조. **19영업일 100% RANGE_BALANCED** |

### 폴러 6개 — 주기·벽시계 위상 (2026-08-01 확정, `mahdi/main.py` "폴러 위상 계획" 주석 참고)

| 폴러 | 주기 | 위상 | 발사 분(mod 10) | 콜/사이클 |
|---|---|---|---|---|
| `poll_option_chain` | 60초 | 0초 | 매 분 | 30(짝)/10(홀) — 위클리 2북은 격분만 |
| `poll_expiry_liquidity` | 60초 확인 | 15초 | 1, 3, 5 (북별 1개) | 11 |
| `poll_investor_flow` | 60초 | 40초 | 매 분 | 3 |
| `poll_macro_snapshot` | 300초 | 168초(2:48) | 2, 7 | ≤7(항목별 주기 분리) |
| `poll_account_balance_cycle` | 300초 | 288초(4:48) | 4, 9 | 1 |
| `poll_signal_fusion_cycle` | 60초 | 10초 | 매 분 | 0 (DB만) |
| `poll_market_halt_heartbeat` | 300초 | — | — | 0 (DB만) |
| `poll_ws_heartbeat` | 300초 | — | — | 0 (DB만, 2026-08-01) |

- 위상은 **벽시계 자정 기준**이다(`_seconds_until_next_wall_tick`). 2026-07-31까지는 기동 시각
  기준이라 매일 달라졌고, 그게 스태거링 설계가 실현되지 않던 원인이었다.
- 밀렸을 때는 **원래 격자로 스냅**하고(`_advance_fixed_tick`), 건너뛴 분은 **먼슬리 10레그로 회수**한다.

### 총 REST 수요 예산 (공유 `_RateLimiter` 1.0건/초 = 용량 100%)

| | 07-30 | 07-31 실측 | 08-01 구현 반영 후(예상) |
|---|---|---|---|
| 총 호출/일 | 19,606 | **12,947** | ~13,400 |
| 초당 수요 | 0.663 (66.3%) | **0.436 (43.6%)** | ~0.452 (45.2%) |
| 적자 시작 백오프 배율 | 1.51배 | **2.28배** | ~2.21배 |

**폴러를 추가할 때는 이 예산부터 확인할 것.** 목표는 50% 이하 유지.

### 알려진 미해결 항목
- **판단 무반응**: 레짐 1종류·확신도 1값·전략 1개(`wait_and_see`)로 19영업일째 고정.
  해소 예정 — `rv_ratio` 2026-08-04경, HMM 학습 2026-08-10경.
- **mod10=0 구간의 "호출 1건당 8~9초" 정체**(07-31 7건) — 원인 미규명, 08-01에 계측만 투입.
  같은 시각대에 `RemoteProtocolError` 8건(서버가 응답 없이 연결 종료)이 있었다 — 08-03에 함께 볼 것.
- **Slack 알림 비활성**(`slack_alert_settings.enabled=false`, 07-28부터) — **2026-08-01 사용자
  결정으로 보류 확정**(개발 단계라 개발자가 항상 모니터링 중). 실거래 전환 검토 시 자동 재검토.
  그래서 생존 신호의 1차 소비자는 Slack이 아니라 **COCKPIT 배지**로 설계한다.
- **COCKPIT 브라우저 육안 검증**이 07-06부터 이월 중(로그에는 기동 라인만 남는다).

### 운영 점검 리듬 (2026-08-01 자동화됨)
- **장마감 자동 집계**: `stop_mahdi_marketclose.bat` → `scripts/daily_ops_report.py` →
  `docs/동작점검/auto/YYYY-MM-DD_지표.{md,json}`. 표와 전일 델타까지 자동, **해석은 사람**.
- **가설 검정**: fix 구현 시점에 `docs/동작점검/hypotheses.yaml`에 예측치를 적으면 다음 거래일
  리포트 §0이 자동 대조한다. `상태`는 자동으로 안 바뀐다(사람이 확정).
- 사람 보고서는 `docs/동작점검/YYYY-MM-DD_마흐디_운영점검보고서.md`에 계속 쓴다.
- 규약 전문: `docs/동작점검/README.md`.
- 이 방식은 07-31에 처음 작동했다(예측 0.438건/초 vs 실측 0.436 — 오차 0.5%).

### 관측 품질 지표 (COCKPIT "오늘의 점검 요약" 2행 그리드)
인프라 11종 + **관측 품질 5종**(REST 수요/백오프 여유/먼슬리 커버리지/밀림 누적/WS 생존).
07-31에 **인프라 지표는 전부 좋아졌는데 먼슬리 커버리지는 후퇴한**(밀림 83→46건 vs 95.0→90.3%)
사례가 있었다 — 두 그룹을 나란히 놓아야 그 어긋남이 보인다.

### 안전장치 생존 신호 (2026-08-01 §5-4 원칙 적용 완료)
| 안전장치 | 생존 신호 | 임계 |
|---|---|---|
| KRX CB 감지 | `market_halt_status.updated_at`(독립 하트비트 300초) | 600초 |
| CircuitBreaker(내부 킬스위치) | `risk_snapshots.cb_state.circuit_breaker_now`(매 사이클 readonly 평가) | — |
| RiskEngine 진입 게이트 | `risk_gate_invocations_today` | **없음**(0회는 정상) |
| WS 연결/재연결 | `ws_status.updated_at`(독립 하트비트 300초) | 600초 |

원칙: **생존 신호는 감시 대상과 독립한 타이머에서 나와야 한다.** 감시 대상 이벤트에 얹으면
"이벤트가 없으면 신호도 멈춰" 죽은 것과 구분되지 않는다(07-30 CB 하트비트에서 실제로 겪음).

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
- `save()`/`load()`(2026-07-10 신규): pickle로 `model`/`state_to_label`을 직렬화 — 오프라인 fit 배치가
  만든 결과를 실시간 프로세스가 재학습 없이 로드
- **알려진 한계**: §7.3 입력 피처에 방향(상승/하락) 신호가 없어 TREND_UP/DOWN 구분은 hurst만으로 확정 불가 (테스트에서도 이 둘은 "트렌드 계열"로만 검증)

### mahdi/engines/regime_pipeline.py — Regime 실시간 배선 (2026-07-10 신규)
- **배경**: `main.py`가 매 분봉마다 `warmup_fallback(RANGE_BALANCED, macro_score=0.0, gap_zscore=0.0)`을
  하드코딩된 인자로만 호출해 레짐이 하루종일 "평균회귀"/`REGIME_UNSTABLE`에 고정되는 버그를 사용자가
  COCKPIT에서 발견해 조사 요청([[SESSION_LOG]]/[[DECISION_LOG]] 2026-07-10 항목 참고).
- `RegimeFeatureBuilder`: 선물봉 롤링 윈도(고/저/종가·스프레드·ATM IV)로 `mahdi/features/regime_features.py`의
  §7.3 6개 피처를 매분 계산.
- `compute_gap_zscore`/`compute_macro_score_proxy`/`latest_prior_close_regime`: `warmup_fallback()` 입력을
  실데이터(`underlying_spot_1m`/`option_analysis_1m`/`investor_flow_1m`/`regime_state`)로 계산.
- `RegimeStateMachine`: 매분 `feature_store`에 피처를 적재하고, `data/models/regime_engine.pkl`(있으면)로
  `RegimeEngine.predict()`, 없으면 실데이터 `warmup_fallback()`을 반환. `main.py`는 선물봉 완성 시에만
  `step()`을 호출한다(이전엔 옵션봉 완성 때도 매번 갱신하던 부수 버그가 있었음).
- **범위 제약(의도적)**: `cross_asset_stress`(USDKRW·USDCNH·US10Y)는 0.0 고정 스텁,
  `macro_score`는 완전한 매크로 나침반(§8) 대신 외국인 순매수 부호 근사치 — 둘 다 TODO로 명시됨.
  (2026-07-10 갱신: USDCNH/US10Y/VIX 원시 데이터 자체는 `macro_snapshot_5m`으로 수집되기 시작했지만
  — 아래 "mahdi/data/overseas_future_master.py + macro_snapshot_5m" 절 참고 — `cross_asset_stress()`
  함수를 그 데이터로 교체하는 배선은 아직 안 됨.)
- `scripts/fit_regime_engine.py`(신규): `feature_store` 축적 데이터로 `RegimeEngine.fit()`을 오프라인
  실행하는 배치. main.py는 refit하지 않고 결과 파일 존재 여부만 본다. 20영업일(~8,000행) 이상 쌓인 뒤
  수동 실행 권장.
- **아직 재시작 안 함** — 반영하려면 관측 루프 프로세스 재시작 필요([[NEXT_TODO]] 참고).

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
  - **2026-07-10 정정(2단계)**: ① 위클리 콜/풋이 상품종류 N/O 단독이 아니라 L/M 풀도 존재함을 발견(2026-07-06엔 그 주 우연히 N/O만 있어 "L/M 없음"으로 오판) → 처음엔 "같은 상품의 교대 코드풀"로 보고 병합 조회했다가, ② COCKPIT이 표시한 실제 만기(2026-07-13=월요일)로 N/O=위클리(월)·L/M=위클리(목)인 **별개 상품**임이 드러나 `series="weekly_mon"`(N/O)/`"weekly_thu"`(L/M) 두 값으로 분리. `main.py`도 위클리 매니저를 둘로 나눠 3북(먼슬리+위클리월+위클리목) 동시 구독, WS 슬롯 예산을 맞추려 `STRIKES_EACH_SIDE`를 3→2로 낮춤(10×3+1=31/41). 상세는 [[DECISION_LOG]]/[[SESSION_LOG]] 2026-07-10 항목, N/O·L/M 요일 매핑의 추가 교차검증은 [[NEXT_TODO]]에 남은 항목.
  - **2026-07-22 정정(후속 프로젝트 messiah 실측)**: 선물 행(정규 "1"·미니 "B")의 실제 월물랭크는 `월물구분코드`가 아니라 `ATM구분`에 들어있다(선물 행에서 `월물구분코드`는 항상 공란) — 기존 코드가 결과적으로 맞는 값을 낸 건 파일이 우연히 근월순 정렬이었기 때문. `futures()`/`front_month_future_code()`를 `ATM구분` 기준 정렬로 수정하고, 그동안 없었던 미니선물 상품종류 `PRODUCT_TYPE_MINI_FUTURES="B"`를 추가(둘 다 `product_type` 파라미터로 선택). 상세는 [[DECISION_LOG]] 2026-07-22 항목.

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
- **2026-07-06 만기 유동성 비교 패널**(Phase 1.5-④): `expiry_liquidity_panel.py`(Plotly Table)가 먼슬리/위클리 두 북의 ATM±2 %스프레드·깊이·거래량·잔존일수를 나란히 표시, `app.py` "만기 유동성 비교" 섹션에 배치. `data_source.py`의 `expiry_liquidity` 필드/`db.latest_expiry_liquidity()`가 공급.
- **2026-07-10 먼슬리 만기 주 안내 추가**: `build_expiry_liquidity_table(rows, today=...)`에 regular 북 만기가 오늘과 같은 ISO주에 속하는지 판정하는 `_is_monthly_expiry_week()` 추가 — 해당되면 표 제목에 "이번 주는 먼슬리 만기 주 — 위클리(목) 신규 상장 없음(위클리(월)은 영향 없음)" 안내 표시(`app.py`가 `snapshot.as_of.date()` 전달). 사용자가 eFriend 캡처로 "먼슬리 만기 주엔 목요일 위클리가 대신 먼슬리로 나온다"를 지적한 데서 비롯.
- **2026-07-10 위클리 월/목 분리**: 사용자가 COCKPIT 만기유동성비교 결과를 보고 "위클리를 월요일/목요일로 나눠 표시해달라" 요청 — `_SERIES_LABEL_KO`에 "위클리(월)"/"위클리(목)" 추가, 세 번째 행으로 나란히 표시. `data_source.py`의 합성 폴백 스냅샷에도 위클리(목) 행 추가. 상세는 위 `symbol_master.py`/`main.py` 항목, [[DECISION_LOG]]/[[SESSION_LOG]] 참고.
- **2026-07-10 화석 series 필터**: 위 분리를 반영해 재시작한 뒤 분리 전 구코드가 쓰던 `series='weekly'` 화석 행이 COCKPIT에 계속 노출되는 문제 발견 — `db.latest_expiry_liquidity()`에 `_VALID_EXPIRY_LIQUIDITY_SERIES`(regular/weekly_mon/weekly_thu) 화이트리스트 필터 추가로 차단(Flow Radar의 `_LEGACY_MIXED_SYMBOL`과 동일 패턴). DB에 남은 옛 행 자체는 안 지워짐 — 완전 삭제는 사용자 확인 필요([[NEXT_TODO]] 참고).

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
- `.venv/Scripts/python.exe -m pytest` — 225개 전부 통과 (2026-07-10 기준, cross-asset stress 매크로
  스냅샷 신규 구현으로 레짐 파이프라인 배선(199개) 이후 26개 추가). **주의**: 이 PC의 기본
  `python`(conda `py37_32`, 3.7)은 `typing.Protocol` 미지원이고 `hmmlearn`도 없어
  `tests/test_engines_regime.py`/`tests/test_regime_pipeline.py` 임포트부터 실패한다 — 반드시 프로젝트
  로컬 `.venv/Scripts/python.exe -m pytest`로 실행할 것.

### mahdi/data/overseas_future_master.py + macro_snapshot_5m — Cross-asset stress 원시 데이터 (2026-07-10 신규)
- `overseas_future_master.py`: KIS 해외선물 마스터파일(`ffcode.mst`, 고정폭 포맷) 다운로드·파싱 —
  품목코드(VX/CNH/ZN 등)별 근월·차근월 단축코드를 최근월물여부 플래그로 자동 산출(`symbol_master.py`와
  같은 패턴, 만기 롤오버 코드 하드코딩 불필요).
- `db/migrations/006_macro_snapshot.sql` + `007_macro_snapshot_zn.sql`: `macro_snapshot_5m` 하이퍼테이블
  — `vix_front`/`vix_next`/`vix_term_structure`(CBOE VX 선물)/`usdcnh`(HKEx CNH 선물)/`us10y_yield`
  (해외주식 국채구분 I 일봉, 실제 수익률 %)/`zn_front`(CME/CBOT 10년 국채선물 근월가, 007에서 추가 —
  수익률과 역상관이라 `us10y_yield`와 단위가 달라 별도 컬럼).
- `mahdi/main.py` `poll_macro_snapshot()`: 5분 주기 폴러, VIX/USDCNH/ZN 개별 실패를 허용하고 VIX·USDCNH
  셋 다 실패할 때만 그 사이클 적재를 건너뜀. `_log_kis_call_failure()`가 실패 시 KIS 에러 바디(rt_cd/
  msg_cd/msg1)까지 로깅.
- `mahdi/dashboard/panels/macro_panel.py`: COCKPIT "Cross-asset Stress" 섹션, 콘탱고/백워데이션 부호 표시.
- **CBOT 계좌 게이트(미해결)**: `zn_front`는 계좌에 CME/CBOT 거래소 신청이 완료돼야 조회된다. 사용자가
  2026-07-10 신청을 완료했다고 확인했지만, 재시작 후 세 사이클(13:12/13:17/13:21) 모두 여전히
  `EGW00552: CBOT SUB거래소 신청 상태가 아닙니다`로 거부됨(실제 KIS 에러 바디로 확인) — KIS 쪽 처리
  지연으로 추정. **코드는 완성 상태**라 실제로 열리면 재시작 없이 다음 5분 사이클부터 자동으로
  채워진다([[NEXT_TODO]] 참고).
- **아직 안 된 것**: `mahdi/features/regime_features.py`의 `cross_asset_stress()`는 여전히 0.0 고정
  스텁이다 — 이번 작업은 원시 매크로 데이터를 `macro_snapshot_5m`에 모으고 COCKPIT에 표시하는 것까지만
  했고, 레짐 엔진 §7.3 입력으로 실제 배선하는 건 별도 작업으로 남아있다([[NEXT_TODO]]/[[DECISION_LOG]]
  참고).

### 2026-07-09 REST 폴링 안정화 (7/8 하루치 실측 기반, [[SESSION_LOG]]/[[DECISION_LOG]] 참고)
- `mahdi/broker/rest_client.py`: `KISRestClient`에 스레드 안전 공유 레이트리미터(기본 2건/초) 추가 — 옵션체인/수급/유동성 폴링 3개 루프가 동시에 REST를 쏘면서 KIS 앱키 TPS 한도를 넘겨 정규장 405분 중 203분치 `option_analysis_1m`이 통째로 유실됐던 문제 대응.
- `mahdi/main.py`: `poll_option_chain`/`poll_investor_flow`에 사이클 전체 실패 시 5초 후 1회 재시도 추가(`CYCLE_RETRY_BACKOFF_SECONDS`).
- (해소, 2026-07-09 2차 수정) 레이트리미터 도입 후에도 남아있던 잔여 유실(5분 간격, 405분 중 4분) — `poll_option_chain`/`poll_expiry_liquidity`/`poll_investor_flow` 세 루프를 "작업 후 sleep"에서 절대시각 고정 틱(`next_tick`) 스케줄링으로 전환, `poll_expiry_liquidity`에 `startup_offset_seconds=30.0`(`EXPIRY_LIQUIDITY_STARTUP_OFFSET_SECONDS`) 추가해 `poll_option_chain`과의 레이트리미터 큐 충돌 빈도를 낮춤([[DECISION_LOG]] 참고).
- **다음 확인 필요**: 정규장 하루 운영 후 데이터 공백 비율(원래의 대량 유실 + 이번 5분 간격 잔여 유실 둘 다)이 실제로 줄었는지 DB로 재확인(NEXT_TODO 참고).

### 알려진 자잘한 문제
- (해소, 2026-07-09) `find_gamma_flip`의 vollib RuntimeWarning/빈 줄 출력 — 원인은 `vollib.ref_python.d1()`의 디버그용 `print('')`(iv/t_years=0 경계 조건). `redirect_stdout`+`catch_warnings`로 국소 억제 완료.
