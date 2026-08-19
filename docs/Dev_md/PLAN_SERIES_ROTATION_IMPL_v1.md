# 종목 로테이션 규칙 구현계획 v1 — SERIES_ROTATION_RULE_v1 를 코드에 녹인다

- 작성일: 2026-08-18 [MW0601]
- 대상 스펙: `docs/Dev_md/SERIES_ROTATION_RULE_v1.md` (검증 완료 29/29, 미구현)
- 상태: **Phase A·B 구현 완료(2026-08-18 장후, 전체 테스트 1698건 통과), Phase C·D 대기.**
  이 문서는 스펙을 마흐디 코드에 배선하는 순서·손익·위험을 정의한다. 스펙 자체의 근거는
  원문에 있고 여기서 반복하지 않는다. 마이그레이션 033은 장전 기동 스크립트의 전체 재적용
  루프(start_mahdi_premarket.bat)가 다음 기동 때 자동 반영한다 — 수동 psql 불요.
  Phase C(파일럿) 착수 조건: A 배포 후 2~3거래일 로그에서 `target_series` 판정이 스펙 표와
  일치하는지 확인한 뒤.

---

## 0. 조사 결과 — 스펙이 가정하지 않은 코드의 현실 세 가지

계획을 세우기 전에 코드를 실측했다. 스펙 문서가 언급하지 않은(또는 몰랐던) 전제 세 개가
드러났고, 이것이 계획의 형태를 정한다.

### 0-1. `option_analysis_1m`에 `series` 컬럼이 없다 — 규칙의 입력 자체가 결손

스펙의 `target_series_for(today, observed_expiries_by_series)`는 **series별 관측 만기**를
입력으로 요구한다. 그런데:

- `_OPTION_ANALYSIS_1M_COLUMNS`(db.py:71)에 `series`가 없다. 유니크 키도
  `(timestamp, underlying, expiry, strike, option_type)`.
- 수집 루프는 레그를 펼칠 때 series를 안다(main.py:1951 `legs` 튜플의 첫 원소) —
  **적재 직전에 버린다.**
- 선택기가 받는 체인(`_chain_snapshot`, db.py:1069)에도 당연히 series가 없고,
  `ChainLeg`(instrument_selection.py:49)에도 없다.

series↔expiry 매핑을 가진 유일한 테이블은 `expiry_liquidity_1m`인데, 그 폴러는 08:31부터
5분 주기라(hypotheses.yaml 2026-08-06-p2) 장전·장초반에 공백이 있고, 스펙의 원칙
("마흐디 자신이 관측한 값을 쓴다 — 같은 체인에서")과 어긋난다. **따라서 마이그레이션으로
`series`를 체인 테이블에 싣는 것이 규칙 구현의 전제조건이다**(§A-1).

### 0-2. 규칙 1(만기 당일 최우선)은 기존 `select_book()`의 명시적 설계와 충돌한다

현행 `select_book()`(instrument_selection.py:177)은 **만기 당일 북을 의도적으로 제외**한다
— "만기 당일 북은 0DTE 플레이북 전용이라 일반 전략의 후보에서 제외한다(§11.4/§11.5).
잔존만기 0일에는 감마플립이 정의되지 않고 핀 리스크만 의미를 갖는다."

로테이션 스펙의 규칙 1은 정반대로 **만기 당일 북을 최우선 목표**로 삼는다(08-18 실측
75배 거래량이 근거). 둘 다 문서화된 결정이고, 나중 것(로테이션)이 앞의 것을 뒤집는다는
명시가 스펙에 없다 — 이것은 구현 디테일이 아니라 **스펙 충돌**이다.

**이 계획의 결정**: 로테이션 스펙을 따르되(규칙 1 구현), 만기 당일 후보에는 기존 0DTE
안전장치를 그대로 물린다. 근거:

1. 08-18의 165건 실패가 정확히 이 충돌의 산물이다 — 만기 당일(weekly_mon, 화요일로 이월)
   을 제외하니 선택기가 유동성 없는 weekly_thu로 갔다. 규칙 1 없이는 스펙의 존재 이유
   (실패 축소)가 절반이 사라진다.
2. 0DTE 위험은 이미 별도 층이 통제한다 — `is_expiry_day()`가 `exit_stack`에
  `EXPIRY_DAY_0DTE` 파라미터(stop -0.008, time_stop 15분, size_cap 0.5)를 물리고,
  `weekly_0dte` 파라미터(strategy_params.yaml)가 사이즈를 깎는다. "만기 당일 북 제외"는
  그 층들이 없던 시절의 방어였고, 지금은 이중 방어의 바깥쪽 겹이다.
3. 단, ADVISORY 모드인 지금은 어느 쪽이든 실주문이 없다 — **후보 생성 기록이 쌓이는 것**이
   전부이고, 그 기록이 CONFIRM 승격 전에 이 결정을 재검증할 데이터가 된다.

이 결정은 되돌리기 쉽게 만든다: `select_book()`의 0DTE 허용을 상수 하나로 분리해,
재검증에서 뒤집히면 규칙 1만 끄고 규칙 2~4는 살린다.

### 0-3. 위클리 REST 폴링은 격분 교대라 "+4레그"는 목표 위클리 분에만 발생

위클리 월=짝수분/목=홀수분 격분(main.py:3543 주석). 사이클당 REST 레그는
먼슬리 10 + 그 분의 위클리 10 = 20이고, 파일럿(§C)에서 목표 위클리를 ATM±3으로 넓히면
**목표 위클리 차례인 분에만 24레그**가 된다(비목표 분은 20 그대로). 스펙 §6-2의 "24개"
계산과 일치하며, 예산 부담이 매분이 아니라 격분이라는 점은 파일럿 위험을 절반으로 줄인다.

---

## 1. 구현손익 조사 — 무엇을 얻고, 무엇을 지불하는가

### 1-1. 효익 (전부 실측 근거)

| 효익 | 근거 | 크기 |
|---|---|---|
| 진입 후보 생성 실패 축소 | 08-18 12:30 점검 — 165건 중 163건 `no_strike_match`, 원인은 유동성 없는 북 선택 | **후보 파이프라인이 사실상 죽어 있던 날들이 되살아난다** — CONFIRM 승격의 전제 |
| 목표북 적중률 | 29/29 (100%), 30거래일 검증(`verify_series_rotation_strategy.py`) | 규칙 오판으로 인한 잘못된 북 선택이 관측 구간에선 0 |
| 만기 당일 오판 제거 | 08-18 실측 weekly_mon 1,184,054 vs weekly_thu 15,843 (75배) | 공휴일 이월 만기일에도 자동으로 맞는 답 |
| 델타 0.2~0.3 도달(파일럿) | v2 회귀가 4날개 중 3날개 무너짐 — 원인은 데이터(창 고착) | `small_strangle_buy` 전략이 처음으로 실측 가능해짐 |
| 사후 검증 가능성 | §3 로깅(target_series/reason/거래량 1위) | 규칙이 낡는 시점을 로그가 자동으로 알려줌 — "이틀치 로그 겹쳐 읽기" 노동 제거 |
| 창 고착 상시 진단(§B) | 08-04 이전 사고(하루치 체인이 5.5% OTM 방치) 재발 감지 | 재발 시 실시간 WARNING — 과거엔 사후 부검으로만 발견 |

### 1-2. 비용

| 항목 | 규모 | 비고 |
|---|---|---|
| 마이그레이션 1건(033, series 컬럼) | 작음 | ADD COLUMN, 기존 행 NULL — 재적재 불필요(§A-1) |
| 코드 변경 | 중간 | 순수 함수 위주(선택기), 수집 루프는 row에 키 1개 추가 |
| 신규 컬럼/필드 | 없음(DB) | §3 로깅은 기존 `selected_instruments` JSONB에 싣는다 — 마이그레이션 불요 |
| WS 슬롯 여유 축소(파일럿만) | 31/41 → 35/41 | 여유 10→6. 재연결 과도기 중복 흡수 가능(스펙 §6-1) |
| REST 예산(파일럿만) | 격분 +4레그 | 1주차에 실측 후 절충안 판단(스펙 §6-2 순서 준수) |
| 테스트 작성 | 중간 | 스펙 §5가 케이스를 이미 표로 고정 — 설계 비용은 선지불됨 |

### 1-3. 위험과 완화

| 위험 | 완화 |
|---|---|
| 규칙 2 표본 1사이클(08-11~13) — 다음 먼슬리 주에 재현 안 될 수 있음 | §3 로깅(목표 vs 거래량 1위 괴리)이 자동 감시. 다음 먼슬리 만기 주 후 `verify_series_rotation_strategy.py` 재실행(§D). 불일치 시 규칙 2만 제거 — 규칙별로 분리 구현해 부분 롤백 가능하게 |
| 0DTE 후보 허용(§0-2 결정)이 과할 수 있음 | ADVISORY라 실주문 없음. 기록으로 재검증 후 CONFIRM 전 확정. 상수 분리로 규칙 1만 OFF 가능 |
| 목표북 미관측 시 침묵 전환 | 스펙대로 `REASON_NO_ELIGIBLE_BOOK`으로 후퇴 — 다른 북으로 조용히 넘어가지 않음 |
| series NULL 과도기(배포 직후 5분 창에 구행 잔존) | 장후 배포(스펙 §5 "장후 적용용"). NULL series 행은 매핑에서 제외하되 레그 자체는 유지 |
| 파일럿 WS 41 초과 / 창 고착 / 예산 초과 | 스펙 §6-6 롤백 조건 그대로 코드화. `pilot_end_date` 무조건발동일로 자동 원복 |
| 수집 우선순위와 거래 목표북 혼동 | 플래그를 합치지 않는다(스펙 §2-1). `OPTION_CHAIN_PRIORITY_SERIES="regular"` 불변, 이름도 구분 유지 |

**손익 판정: 실행한다.** 비용의 대부분은 순수 함수와 JSONB 필드라 되돌리기 싸고, 효익은
후보 생성 파이프라인 소생이라는 CONFIRM 승격의 하드 전제다. 단 파일럿(§C)은 레버+자동
만료로 격리해, 본체(§A·§B)와 손익 계산을 분리한다.

---

## 2. 단계별 구현계획

의존 순서: **A(규칙 본체) → B(창 고착 계측) → C(파일럿) → D(재검증)**.
A와 B는 독립이라 병행 가능하지만, C는 A(목표북 판정)와 B(계측)를 둘 다 전제한다
(스펙 §6-4 0단계 "계측 먼저").

### Phase A — 로테이션 규칙 본체 (선택기 + series 배선)

#### A-1. 마이그레이션 033 — `option_analysis_1m.series`

```sql
ALTER TABLE option_analysis_1m ADD COLUMN IF NOT EXISTS series VARCHAR(16);
```

- 값: `"regular" | "weekly_mon" | "weekly_thu"` (`_VALID_EXPIRY_LIQUIDITY_SERIES`와 동일 어휘,
  db.py:1232 — 새 series 추가 시 그 튜플과 함께 갱신하라는 기존 규약을 따른다).
- 기존 행은 NULL로 둔다. **백필하지 않는다** — 선택기가 보는 창은 5분(`CHAIN_SNAPSHOT_MAX_AGE_MINUTES`)
  이라 배포 다음 사이클부터 채워지고, 과거 행은 어차피 선택기 입력이 아니다.
  (사후 분석용 백필이 필요해지면 `expiry_liquidity_1m`의 series↔expiry로 유추하는 스크립트를
  그때 별도로 — 지금 하면 유추를 사실처럼 굳힌다.)
- 유니크 키는 그대로 — 같은 (expiry, strike, type)이 두 series에 동시에 있을 수 없다
  (만기가 곧 북을 정한다).

#### A-2. 수집 루프 — row에 series 1키 추가

- `mahdi/main.py` 폴링 루프(1973행대): `_parse_option_quote()`가 돌려준 `row`에
  `row["series"] = series`를 넣는다(루프 변수로 이미 갖고 있다 — 지금은 버리는 값).
- `_OPTION_ANALYSIS_1M_COLUMNS`(db.py:71)에 `"series"` 추가.
- **수집 우선순위는 건드리지 않는다**: `OPTION_CHAIN_PRIORITY_SERIES`(main.py:468),
  `is_priority` 판정, 실패 예산·조기 포기 로직 전부 불변(스펙 §2-1).

#### A-3. 체인 스냅샷 → `ChainLeg`로 series 전달

- `_CHAIN_SNAPSHOT_SQL`(db.py:1069) SELECT 목록에 `series` 추가, 반환 dict에 `"series"` 키
  추가. `_restrict_to_latest_cycle_window()`는 컬럼 순서에 의존하므로(db.py:1084 주석)
  언패킹 위치를 함께 갱신 — **테스트가 이 순서 의존을 잡도록 케이스 추가**.
- `ChainLeg`에 `series: str | None = None` 추가, `legs_from_chain_snapshot()`에서 채운다.
  NULL(구행·백테스트 픽스처)은 None으로 보존 — "모른다"와 "regular"를 구분한다.
- `legs_from_chain_rows()`(options_intel.py)는 키를 명시적으로 골라 읽으므로 무영향
  (rv_5d 때 확인된 성질, db.py:1130 주석).

#### A-4. `target_series_for()` 신설 + `select_book()` 교체 — 핵심

`mahdi/fusion/instrument_selection.py`:

```python
def observed_expiries_by_series(legs) -> dict[str, date]:
    # series가 None인 레그는 제외. 같은 series에 만기 여럿이면(이월 잔존) 가장 가까운 미래·당일.

def target_series_for(today: date, expiries: dict[str, date]) -> tuple[str | None, str]:
    # 스펙 §2-2 의사코드 그대로. 반환 2번째 값은 사유("rule1_expiry_day" |
    # "rule2_monthly_week" | "rule3_tue_thu" | "rule4_fri_mon" | "no_rule").
    # 규칙 1은 상수 ALLOW_EXPIRY_DAY_TARGET(기본 True)로 분리 — §0-2 결정의 되돌림 지점.

def select_book(legs, today) -> tuple[date | None, str | None, str | None, str | None]:
    # 반환: (book_expiry, series, target_reason, fail_reason)
    # 1) target_series_for()로 목표 series 결정
    # 2) 그 series의 관측 만기를 채택 — 스펙대로 "series 먼저, 만기는 그 결과"로 순서 반전
    # 3) 목표 series가 체인에 미관측이면 (None, series, reason, REASON_NO_ELIGIBLE_BOOK)
    #    — 다른 북으로 조용히 넘어가지 않는다
    # 4) 전 레그 series=None(구행만 남은 과도기·백테스트 구픽스처)이면 기존 최근접 규칙으로
    #    폴백하되 target_reason="fallback_nearest"로 그 사실을 기록 — 침묵 폴백 금지
```

주의점:

- **`is_expiry_day()`와의 정합**: 규칙 1로 만기 당일 북이 목표가 된 날은 `is_expiry_day()`도
  참이다(같은 관측에서 나온다). exit 쪽이 0DTE 파라미터를 무는 것을 테스트로 못박는다 —
  두 곳이 만기 여부를 따로 판정하면 안 된다는 기존 규약(instrument_selection.py:167 주석)
  그대로.
- 규칙 2의 ISO 주 비교는 `date.isocalendar()[:2]` — 스펙 명시 그대로, 요일 근사 금지.

#### A-5. `select_instruments()`·기록 배선

- `SelectionResult`에 `target_series: str | None`, `target_series_reason: str | None`,
  `volume_leader_series: str | None` 추가, `to_record()`에 포함 →
  기존 `selected_instruments` JSONB(마이그레이션 031)에 그대로 실린다. **DB 마이그레이션 불요**
  (스펙 §3의 "컬럼 또는 JSONB" 중 JSONB 선택 — 이유: 이미 선택기 기록의 단일 적재점이고,
  컬럼 추가는 파일럿 검증 후 집계 쿼리가 실제로 필요해질 때).
- `volume_leader_series`: 그 분 체인에서 series별 `volume` 합의 1위(None 제외).
  스펙 §3의 사후 검증용 — 목표와 실측 1위가 자주 갈리면 규칙이 낡았다는 신호.
- 진입 전략이 없어 선택기를 안 돌리는 분(main.py:4133 분기)에도 target_series는 계산해
  기록한다 — 규칙 검증 표본은 진입 후보 유무와 무관하게 매분 쌓여야 §D 재검증이 된다.

#### A-6. 회귀 테스트 (`tests/test_fusion_instrument_selection.py` 확장)

스펙 §5가 지정한 케이스를 그대로 고정:

1. 만기 당일 최우선 — **공휴일로 화요일에 이월된 만기**(08-18 재현: 화요일인데 weekly_mon
   이 목표). 이번에 놓쳤던 사례 1.
2. 먼슬리 만기 주 화·수·목 → `regular`.
3. **먼슬리 만기 주 월요일 → `weekly_mon`**(규칙 2가 월요일을 안 먹는 것). 놓쳤던 사례 2.
4. 일반 화·수·목 → `weekly_thu`, 금·월 → `weekly_mon`.
5. 목표북 미관측 → `REASON_NO_ELIGIBLE_BOOK`(침묵 전환 없음).
6. 전 레그 series=None → 최근접 폴백 + `fallback_nearest` 기록.
7. `_restrict_to_latest_cycle_window` 컬럼 순서(A-3).
8. 규칙 1 목표일에 exit 쪽 `is_expiry_day()`가 참(0DTE 파라미터 정합).

#### A-7. 배포

- 장후(15:45 이후) 적용 — 스펙 §5 명시. 마이그레이션 → 코드 순.
- 다음 거래일 장전 점검에서 확인할 것: (1) `option_analysis_1m.series` 채워짐,
  (2) `selected_instruments`에 target_series/reason 실림, (3) 그날 요일 기준 목표북이
  스펙 표와 일치.

### Phase B — 창 고착 상시 계측 (스펙 §6-3, 파일럿 전제이자 독립 가치)

- 위치: `_reroll_books_to_spot()`(main.py:1114) — 매 사이클 각 매니저에 대해
  `|spot - manager.current_atm|`을 잰다(`current_atm` 프로퍼티가 이미 있다,
  subscription_manager.py:269).
- 판정: 거리 > `strike_interval * ATM_ROLL_HYSTERESIS_RATIO`(2.5×0.75=1.875)인 상태가
  **직전 사이클에도 있었고 줄지 않았으면** — 롤링이 걸려야 하는데 안 걸린 것 — WARNING
  1줄(사이클당 최대 1줄, 로그 폭증 방지 관례 준수).
- 직전 거리 상태는 `WsLiveness`에 북별 dict로 든다(atm_roll_count와 같은 자리).
- 파일럿과 무관하게 **상시 ON** — 08-04 이전 사고의 재발 감지기.
- 테스트: 고착 시나리오(거리 유지 2사이클)에서 WARNING 1회, 정상 롤링에서 0회.

### Phase C — 구독 창 확장 파일럿 (스펙 §6, 레버 + 자동 만료)

**전제: A 배포 후 최소 2~3거래일 관측으로 target_series 판정이 로그에서 검증된 뒤 시작.**

#### C-1. 레버 (`strategy_params.yaml`)

스펙 §6-5 그대로:

```yaml
weekly_window_pilot:
  enabled: true
  target_series_strikes_each_side: 3
  pilot_end_date: "2026-08-29"   # 무조건발동일 — 지나면 자동 OFF
```

- `get_strategy_params()`는 `@lru_cache`지만 프로세스가 매일 장전 재기동하므로
  (settings.py:110 주석 관례) 파일 반영은 다음 기동에 자연히 된다. 단 **날짜 비교는 매
  사이클 런타임에** 한다 — 캐시된 설정이라도 `pilot_end_date < today`면 그 자리에서 OFF.
- 파일럿 종료 시 별도 배포 없이 ±2로 원복 — "잊힌 유예" 패턴 차단(스펙 §6-5).

#### C-2. 목표북 비대칭 확대

- `RollingSubscriptionManager.set_strikes_each_side(n)` 추가: 값이 바뀌면
  `_current_atm` 기준으로 창을 즉시 재계산(기존 roll 로직 재사용, 히스테리시스 무시하고
  1회 강제 롤) — 안 바뀌면 no-op.
- 호출부: 관측 루프에서 매 사이클, A-4의 `target_series_for()` **같은 함수**로 오늘의
  목표 series를 정하고(입력은 그 세션에서 관측한 series별 만기 — 수집 루프가 이미
  갖고 있는 값으로 인메모리 dict 유지, 기동 직후엔 DB에서 오늘·미래 만기를 1회 시드),
  목표 위클리만 3, 나머지는 2로 세팅. **먼슬리는 확대 대상이 아니다**(스펙 §6-1 표).
- 목표를 아직 못 정한 기동 초기(관측 0)에는 전 북 ±2 유지 — 31/41, 안전한 기본값.
- 슬롯 가드: 세팅 전 `10+10+14+1=35 ≤ MAX_SUBSCRIPTIONS(41)` 정적 확인 + 런타임에
  실제 구독 수가 41 도달 시 즉시 ±2 원복(스펙 §6-6 두 번째 롤백 조건).

#### C-3. REST는 자동 추종 — 변경 없음, 관측만

- `poll_option_chain()`은 `desired_strikes`를 그대로 쓰므로(main.py:1953) 창이 넓어지면
  레그가 저절로 24개(목표 위클리 분)가 된다. **절충안(먼슬리 IV-lite)은 1주차에 넣지
  않는다** — 스펙 §6-2의 순서(넓히기 → 실측 → 필요 시 절충안) 준수. 두 변수를 동시에
  바꾸면 원인 귀속이 안 된다.
- 관측 지표: `budget_exceeded` 시간대별 비율(기존 게이지), 경고선 0.8 초과 시 절충안
  앞당김(스펙 §6-6 세 번째 조건).

#### C-4. 롤백 조건 코드화 (스펙 §6-6)

- 창 고착 WARNING(§B) 1회 → 그날 데이터 분석 제외 태그, 3회 → 파일럿 중단(레버 OFF)
  후 롤링 로직 우선 수리.
- WS 41 초과 관측 → 즉시 ±2 원복(§C-2 가드).
- 판정·조치는 사람이 하되, 조건 도달 자체는 로그가 셀 수 있게 남긴다(수동 집계 금지).

### Phase D — 재검증 및 상설화 판정

- 파일럿 5거래일+(위클리 월·목 각 1사이클, 화~목/금~월 걸침 — 스펙 §6-4) 후:
  - `verify_weekly_delta_reach.py` v2를 넓은 창 실측으로 재실행 — 도달률·R²·**spread_state**.
  - Go/No-Go: 도달률 상승 + 호가 존재 → `STRIKES_EACH_SIDE` 상수 차원의 상설화 검토,
    아니면 레버 만료로 자동 원복.
- 다음 먼슬리 만기 주(9월) 경과 후: `verify_series_rotation_strategy.py` 재실행 —
  규칙 2 재현 확인. 불일치 시 규칙 2만 제거(A-4가 규칙별 분리 구현이라 가능).
- 배포 후 1개월: §3 로그로 target_series vs volume_leader_series 괴리율 집계 —
  괴리가 잦으면 규칙 갱신 신호.

---

## 3. 명시적으로 하지 않는 것

- **수집 우선순위 변경 없음** — 먼슬리 `is_priority=True` 고정, GEX/감마플립 입력은
  요일 무관(스펙 §2-1). "거래 목표북"과 "수집 우선순위"를 한 플래그로 합치지 않는다.
- **먼슬리 IV-lite 절충안 선반영 없음** — 파일럿 1주차 실측이 필요성을 숫자로 확정한
  뒤에만(§C-3).
- **ATM±4 확대 없음** — ±3 실측 후 2차 검토(스펙 §6-1).
- **과거 행 series 백필 없음** — 선택기 입력이 아니고, 유추를 사실로 굳히는 일(§A-1).
- **signal_decisions 전용 컬럼 추가 없음** — JSONB로 시작, 집계 수요가 실증되면 그때.

## 4. 작업량 추정

| Phase | 규모 | 산출물 |
|---|---|---|
| A | 1일 내외 | 마이그레이션 033, 수집 row 1키, 스냅샷 SQL, ChainLeg, target_series_for/select_book, 기록 배선, 테스트 8케이스 |
| B | 반나절 | _reroll_books_to_spot 계측 + WsLiveness 상태 + 테스트 |
| C | 1일 내외 | yaml 레버, set_strikes_each_side, 목표북 세팅 루프, 슬롯 가드, 테스트 |
| D | 코드 0 | 기존 검증 스크립트 재실행 + 판정 문서 |

배포는 A → (2~3거래일 관측) → B → C(파일럿 개시) → D 순. A와 B는 같은 장후에 함께
배포해도 된다(서로 독립, 둘 다 읽기 전용에 가깝다).
