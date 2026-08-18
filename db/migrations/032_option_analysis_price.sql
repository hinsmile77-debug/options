-- Mahdi 추가 (2026-08-18) — 옵션 현재가(프리미엄). Passive-first 지정가의 전제.
--
-- ## 이 컬럼이 없어서 막혀 있던 것
--
-- `option_analysis_1m`에는 그릭스·IV·OI·거래량만 있고 **가격이 없었다.** v6 §13.2의
-- Passive-first 진입은 기준가에서 틱을 빼서 지정가를 만드는 것이므로, 가격 없이는
-- `EntryContext.reference_price`를 채울 수 없고 진입 계획 자체가 나오지 않는다.
-- 2026-08-17 §11.5 선택기를 배선할 때 이 빈칸이 CONFIRM 승격의 하드 전제로 드러났다.
--
-- ## 필드명을 어떻게 확정했나 (추측 금지 — KIS_RAW_FIELD_RANGES.md 체크리스트)
--
-- 2026-08-17에 넣은 «프로세스 1회 output1 키 목록» INFO가 08-18 07:31에 실측을 남겼다.
-- 그 목록에 **`optn_prpr`는 없다.** 가격 계열은 전부 `futs_*` 접두어다:
--
--     futs_prpr(현재가) · futs_oprc/hgpr/lwpr · futs_sdpr(기준가) · hts_thpr(이론가) · acpr(행사가)
--
-- 공식 문서(docs/efriend xlsx)에는 `optn_prpr`(옵션 현재가)가 **존재한다** — 다만 그것은
-- 다른 TR의 응답 스키마이고, 우리가 부르는 TR은 선물옵션 공통 스키마로 답한다.
-- 문서와 실측이 갈리면 실측을 따른다(2026-07-06 `gama`, 2026-07-03 컬럼 순서와 같은 판단).
--
-- **`futs_` 접두어가 «선물의 값»을 뜻하지 않는다는 증거**: `_parse_option_quote()`는 이미
-- `futs_last_tr_date`를 **그 옵션의 만기**로 읽고 있고, 그 값은 08-18 실측에서 08-18/08-20/
-- 09-10 세 만기로 정확히 갈린다. 선물(A01609)의 만기라면 전 행이 09-10이어야 한다.
-- 즉 `futs_*`는 «조회 대상 자신»의 필드이고, 옵션을 조회하면 옵션의 값이 온다.
--
-- ## 자릿수
--
-- 프리미엄은 지수 포인트 단위(원화 절대값이 아니다)라 이론적 상한이 크지 않지만, 실측 범위가
-- 아직 없으므로 `market_raw_1m`의 가격 컬럼과 같은 `DECIMAL(18,4)`로 넓게 잡는다
-- (체크리스트 4항: "실측값이 없으면 임시로 넓게 잡아두고 며칠 실측 후 표에 채운다").

ALTER TABLE option_analysis_1m ADD COLUMN IF NOT EXISTS price DECIMAL(18,4);

COMMENT ON COLUMN option_analysis_1m.price IS
    'KIS output1.futs_prpr — 그 옵션의 현재가(프리미엄, 지수 포인트). 2026-08-18 실측으로 '
    '필드명 확정(응답에 optn_prpr는 없다). NULL이면 그 분에 가격을 못 읽은 것이고, '
    '0.0(체결 없는 심외가)과 구분된다. Passive-first 지정가의 기준가로 쓴다(v6 §13.2).';
