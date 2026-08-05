-- Mahdi 추가 (2026-08-05, 운영점검보고서 2026-08-05 §2 이상점 1 / Fix#1) — 판단 시점의 VRP.
--
-- **왜 필요한가**: 08-05에 `poll_signal_fusion_cycle()`이
--   fusion_engine.evaluate(signal_inputs, MetaLabelContext())
-- 로 호출하고 있는 것이 드러났다 — `evaluate()`의 세 번째 인자 `vrp`가 **한 번도 전달된 적이
-- 없어** 기본값 0.0으로 고정돼 있었다. `strategy_palette._vrp_state(0.0, 0.02)`는 항상
-- "fair"를 돌려주므로, v6 §11.4 매트릭스 3열(저평가/적정/고평가) 중 **2열이 전 이력 도달
-- 불가**였다. 레짐이 21영업일 RANGE_BALANCED 고정이라 행도 하나로 고정돼 있었으니,
-- 9칸짜리 매트릭스에서 실제로 도달 가능한 칸은 `RANGE_TIGHT x fair = ["wait_and_see"]`
-- **한 칸뿐**이었다 — 즉 진입 후보는 구조적으로 나올 수 없었다.
--
-- 피해가 08-05에 처음 눈에 보였다: 그날 앙상블 멤버가 2 → 4로 살아나면서 방향 ±0.692,
-- 동조 3, 확신도 0.75짜리 HIGH_CONVICTION이 6건 나왔는데 **전부 `strategy_palette:wait_only`로
-- 버려졌다.** 신호가 약해서가 아니라 팔레트가 그 신호를 볼 수 없어서였다.
--
-- 배선만 하고 값을 안 남기면 다음 날 "왜 그 셀이었나"를 되짚을 수 없다. 022/023이 gex/
-- gamma_flip/gex_expiry를 판단 행에 남긴 것과 **같은 이유**로 VRP도 남긴다.
--
--   vrp  판단 시점 먼슬리(최근월) 북의 ATM 스트래들 VRP = mean(ATM 콜 IV, ATM 풋 IV) − rv_5d.
--        NULL = 산출 불가(스팟 없음 / 체인 비었음 / ATM 행사가에 콜·풋이 둘 다 있지 않음 /
--        rv_5d 없거나 0 이하). **NULL과 0.0은 다르다** — NULL은 "못 쟀다", 0.0은 "쟀는데
--        IV와 RV가 같다"이고, 전자는 팔레트에서 안전한 쪽(fair=관망)으로 폴백한 분이다.
--        판정 밴드는 `strategy_palette.select_strategies(vrp_neutral_band=0.02)` = ±2 변동성 포인트
--        (iv/rv는 `_parse_option_quote()`가 KIS 원시 %를 100으로 나눠 저장한 분수다).

ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS vrp DOUBLE PRECISION;

COMMENT ON COLUMN signal_decisions.vrp IS
    '판단 시점 먼슬리 북의 ATM 스트래들 VRP(2026-08-05 §2 이상점 1 / Fix#1). = mean(ATM 콜 IV, '
    'ATM 풋 IV) − rv_5d. 08-05까지 evaluate()에 vrp가 전달된 적이 없어 항상 0.0(=fair)이었고, '
    'v6 §11.4 매트릭스 9칸 중 도달 가능한 칸이 1칸뿐이었다. NULL은 산출 불가(fair로 폴백한 분)이며 '
    '0.0(IV=RV)과 구분된다. 콜·풋 중 한쪽만 있으면 산출하지 않는다 — KIS hts_ints_vltl이 행사가 '
    '격자에 따라 계통적으로 튀어(08-05 실측 홀수배 0.57~0.63 vs 5의 배수 0.87~0.89) 단일 레그로는 '
    '행사가가 한 칸 옮겨가는 것만으로 VRP 부호가 뒤집힌다.';
