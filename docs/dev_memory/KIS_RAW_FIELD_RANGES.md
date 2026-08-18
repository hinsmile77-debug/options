# KIS 옵션시세(`inquire-price` output1) 원시 필드 범위 참고표

**만든 이유(2026-07-21, 운영점검보고서 §5-1)**: `theta` 컬럼(`DECIMAL(8,6)`)이 KIS가 실제로
주는 원화 단위 값(수백~수천대)을 담기엔 자릿수가 부족해 상시 오버플로우하던 버그를
2026-07-16에 처음 발견하고도 5일간 근본원인을 몰랐다(원시값 로깅이 없었기 때문). 앞으로 이
클래스의 버그("KIS 응답의 실제 값 범위를 안 보고 DB 컬럼 자릿수를 추측으로 정함")를 다시
겪지 않으려면, 새 필드를 스키마에 추가하기 전에 **먼저 이 표를 확인하거나, 없으면 raw 로그로
실측부터 하고 채워 넣을 것**.

**필드 목록을 확인하는 법(2026-08-17 추가)**: `_parse_option_quote()`가 프로세스당 1회
`logs/observation_loop.log`에 `KIS 옵션시세 output1 필드 목록`을 INFO로 남긴다 — 파싱 실패를
기다리지 않고도 «이 응답에 무슨 필드가 있는가»를 매 기동마다 알 수 있다.

값을 확인하는 법: `mahdi/main.py`의 `_parse_option_quote()`가 파싱 실패 시(또는
`WarningThrottle`로 억제되지 않은 매 실패마다) `_raw_kis_output1`을 `logs/observation_loop.log`에
그대로 남긴다(2026-07-19 도입). 여기 적힌 "관측 범위"는 그 로그에서 실측한 값이며 **KIS
공식 문서에 명시된 절대 상한이 아니다** — 시장 상황에 따라 더 극단적인 값이 나올 수 있으므로
여유를 두고 컬럼 타입을 정할 것.

## `mahdi/main.py`의 `_parse_option_quote()`가 실제로 읽는 필드

| KIS 원시 필드 | 의미 | DB 컬럼(타입) | 관측 범위(2026-07-21 기준) | 비고 |
|---|---|---|---|---|
| `delta_val` | 델타 | `option_analysis_1m.delta` `NUMERIC(8,6)` | \|0.886\|까지 (이론상 [-1, 1]) | 정의상 상한이 있어 안전 |
| `gama` | 감마 | `option_analysis_1m.gamma` `NUMERIC(10,8)` | \|0.0571\|까지 | 안전 |
| `theta` | 세타(원화 단위 하루 시간가치 감소분) | `option_analysis_1m.theta` `NUMERIC(14,4)`(2026-07-21 확장, 원래 `NUMERIC(8,6)`) | \|9625.4268\|까지 | **과거 오버플로우 범인.** 정규화 안 된 원화 절대값 — 얇은/근접만기 옵션일수록 커진다 |
| `vega` | 베가 | `option_analysis_1m.vega` `NUMERIC(8,6)` | 61.3252까지 | 2026-07-21 전수 재검토로 안전 확인했으나, theta처럼 원화 절대값이라 이론적 상한이 없다 — 재발 시 1순위 의심 대상 |
| `hts_ints_vltl` | IV(%) | `option_analysis_1m.iv` `NUMERIC(8,6)` (raw/100으로 저장, `_parse_option_quote`에서 변환) | raw 최대 ~102.9976(%) → 저장값 ~1.03 | %를 분수로 바꿔서 저장하므로 안전 |
| `hist_vltl` | 과거(실현)변동성(%) | `option_analysis_1m.rv_5d` `NUMERIC(8,6)` (raw/100) | iv와 유사한 범위로 추정 | iv와 같은 변환, 안전 |
| `hts_otst_stpl_qty` | 미결제약정(OI) | `option_analysis_1m.oi` `INTEGER` | — | 정수형이라 소수점 오버플로우 없음 |
| `otst_stpl_qty_icdc` | OI 증감 | `option_analysis_1m.oi_change` `INTEGER` | — | 상동 |
| `acml_vol` | 누적거래량 | `option_analysis_1m.volume` `INTEGER` | — | 상동 |
| `futs_last_tr_date` | 최종거래일(만기) | `option_analysis_1m.expiry` `DATE` | — | 문자열 파싱만, 오버플로우 무관. **`futs_` 접두어가 «선물의 값»이 아니라는 증거**: 이 값은 조회한 **옵션 자신의** 만기이고, 08-18 실측에서 08-18/08-20/09-10 세 만기로 갈렸다(선물 A01609의 만기라면 전 행이 09-10이어야 한다) |
| `futs_prpr` | **옵션 현재가(프리미엄, 지수 포인트)** | `option_analysis_1m.price` `NUMERIC(18,4)` (2026-08-18 마이그레이션 032) | 미실측 — 며칠 뒤 채울 것 | Passive-first 지정가의 기준가(v6 §13.2). 자릿수는 실측 전이라 `market_raw_1m` 가격 컬럼과 같게 넓게 잡았다(체크리스트 4항). `main._warn_if_price_looks_like_underlying()`가 이 값이 기초자산 수준이면 첫 사이클에 경고한다 |

## 파싱은 되지만 DB에 저장 안 되는 필드 (참고만)

| KIS 원시 필드 | 의미 | 상태 |
|---|---|---|
| `rho` | 로 | `_parse_option_quote()`가 아예 안 읽음(스키마에 `rho` 컬럼 없음) — Phase2에서 필요해지면 이 표부터 갱신할 것 |
| `hts_thpr` | HTS 이론가 | 미사용. `price`(실제 체결 기준 현재가)와 대조하면 «시장가 vs 이론가» 괴리를 볼 수 있다 — 필요해지면 이 표부터 갱신 |
| `acpr` | 행사가 | 미사용 — 우리는 행사가를 요청 시점에 이미 알고 있다(마스터파일 기준) |
| `futs_oprc` / `futs_hgpr` / `futs_lwpr` / `futs_sdpr` | 시가/고가/저가/기준가 | 미사용 |
| `optn_prpr` | 옵션 현재가 | **우리 TR 응답에는 없다.** 공식 문서(docs/efriend xlsx)에는 존재하지만 그것은 다른 TR의 스키마다 — 2026-08-18 실측 키 목록에 없음을 확인했다. 문서와 실측이 갈리면 실측을 따른다(2026-07-06 `gama`와 같은 판단) |
| `dprt` | 괴리율(%) | 미사용. 전일 종가가 없는 신규상장 종목일 때 `9999.99` 같은 **sentinel 값**을 반환하는 것을 실측(2026-07-21) — 앞으로 이 필드를 쓰게 되면 sentinel 처리부터 넣을 것 |
| `vanna`, `charm`, `skew_25d`, `spread_state` | — | DB 컬럼은 있지만 `_parse_option_quote()`가 항상 `None`으로 채움(Phase1 스텁, 아직 계산 로직 없음) |

## 스키마에 새 필드를 추가할 때 체크리스트

1. 이 필드가 KIS 응답의 **정규화된 값**(비율/분수, 이론적 상한 있음)인지 **원화 절대값**(이론적
   상한 없음, 예: theta/vega처럼 계약 규모·잔존만기에 따라 커짐)인지부터 구분한다.
2. 정규화된 값이면 기존 `NUMERIC(8,6)` 관례를 따라도 대체로 안전하다.
3. 원화 절대값이면 `gex`(`NUMERIC(18,4)`)처럼 넉넉한 정수부를 준다 — 최소한 이 표에 실측값이
   쌓이기 전까지는 좁게 잡지 말 것.
4. 실측값이 없으면(신규 필드) 임시로 넓게 잡아두고, `_raw_kis_output1` 로깅 패턴을 그대로
   재사용해 며칠 실측 후 이 표에 범위를 채워 넣는다.
