-- Mahdi 추가 (2026-09-03, 감마월 정의·매핑 점검 §1 / 이상점 5) — **판단을 실제로 움직인
-- 기준선을 남긴다.**
--
-- **왜 필요한가**: 마이그레이션 022가 `gamma_flip`을 판단 행에 남긴 목적은 «신호가 무엇을
-- 보고 그렇게 판정했는가»를 사후에 세는 것이었다. 그런데 2026-08-04 「결정 2: `options_flow`의
-- 감마플립 의존 해제 — 감마 월 폴백」 이후 **실동작 기준선은 거의 항상 감마 월**이다:
--
--   08-03~08-10 판단 2,944건 중 `gamma_flip` non-NULL은 08-05의 22건뿐, 나머지 전부 NULL
--
-- 즉 022가 남긴 컬럼은 «거의 안 쓰인 쪽»이고, `_options_flow_score()`가 실제로 부호를 낸
-- `spot - reference`의 `reference`는 **DB에 한 번도 남은 적이 없다.** 022 주석이 내건 목적이
-- 이 축에서만 통째로 비어 있었다.
--
-- **왜 재계산으로는 안 되는가**: 체인을 다시 읽어 `gamma_walls()`를 돌리면 될 것 같지만,
-- `_chain_snapshot()`은 5분 이월 창을 쓴다(`CHAIN_SNAPSHOT_MAX_AGE_MINUTES`) — 사후에 같은
-- 분을 조회해도 당시와 같은 레그 집합이라는 보장이 없다. 판단이 본 값은 판단 행에 있어야 한다.
--
-- **두 컬럼인 이유**: 값만 남기면 «그날 flip이 살아 있었는가»를 못 센다. `gamma_flip`이
-- non-NULL인데 게이트(`OPTIONS_FLOW_GAMMA_WALL_FALLBACK`)가 꺼져 있는 경우까지 포함해,
-- 출처는 **점수를 낸 쪽과 같은 함수**(`signal_layer.options_flow_reference()`)가 정한다.
--
--   gamma_flip             (022)  그 분의 감마플립. 산출 불가면 NULL
--   gamma_wall             (037)  그 분의 감마 월(|γ×OI| 최대 행사가). 노출 0이면 NULL
--   gamma_reference_source (037)  둘 중 **실제로 쓴 쪽**: 'flip' | 'wall' | 'none'
--
-- ⚠ `gamma_wall`은 «화면에 그린 선»이 아니라 **판단이 쓴 선**이다. COCKPIT은 낡은 스팟에서
-- 월을 긋지 않지만(09-03 수정 3) 그것은 표시 정책이고, 엔진은 스팟이 5분 경계를 넘으면
-- 아예 `NULL`이 된다(`db.UNDERLYING_SPOT_MAX_AGE_MINUTES`). 두 NULL의 뜻이 다르지 않게
-- 여기 남는 것은 엔진 쪽 하나뿐이다.
--
--   NULL(gamma_wall)  이 컬럼 이전(2026-09-03 이전) 행이거나, 그 분에 월이 없었다
--                     (체인 0레그 / 스팟 미가용 / 최상위 노출 0). 어느 쪽인지는
--                     `gamma_reference_source`와 `chain_leg_count`가 갈라 준다 —
--                     source가 NULL이면 「이 컬럼 이전 행」, 'none'이면 「쟀는데 없었다」다
--                     (029/036 주석과 같은 구분).

ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS gamma_wall DOUBLE PRECISION;
ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS gamma_reference_source TEXT;

COMMENT ON COLUMN signal_decisions.gamma_wall IS
    '판단 시점의 감마 월 — 행사가별 |gamma x OI x 승수 x S^2/100| 합이 가장 큰 행사가 '
    '(options_intel.gamma_walls(top_n=1), 2026-09-03). **부호 없는 집중도**이지 "최대 +GEX '
    '행사가"가 아니다 — 같은 행사가의 콜과 풋은 상쇄되지 않고 더해지며, 순 GEX가 가장 음수인 '
    '행사가가 월이 될 수 있다. 08-04 감마 월 폴백 이후 options_flow의 실동작 기준선이 이 값이다. '
    'NULL=이 컬럼 이전 행이거나 그 분에 월이 없었다(gamma_reference_source가 둘을 가른다).';

COMMENT ON COLUMN signal_decisions.gamma_reference_source IS
    'options_flow가 실제로 기준선으로 쓴 값의 출처: flip=gamma_flip, wall=gamma_wall(08-04 '
    '폴백), none=둘 다 없거나 폴백 게이트가 꺼져 있었다(2026-09-03). 점수를 내는 쪽과 같은 '
    '함수(signal_layer.options_flow_reference())가 정하므로 기록과 판단이 갈릴 수 없다. '
    'NULL=이 컬럼 이전 행.';
