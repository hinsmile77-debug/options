-- Mahdi 추가 (2026-08-03, 운영점검보고서 2026-08-03 §5-1) — 판단 시점의 옵션 체인 입력.
--
-- **왜 필요한가**: 07-31 §5-5가 "인프라 지표가 좋아져도 판단 입력은 나빠질 수 있다"는 원칙을
-- 세우고 `먼슬리 분 커버리지` 지표를 만들었다. 08-03에 그 값은 98.8%로 훌륭했는데, 정작
-- **감마플립은 산출률 0%**였다(전 이력 0건). 커버리지는 *"데이터가 DB에 있는가"* 만 재고
-- *"그 데이터가 신호까지 도달했는가"* 는 재지 않았기 때문이다 — 한 칸이 비어 있었다.
--
-- 판단 시점의 체인 입력을 판단 행에 함께 남기면 두 가지가 가능해진다:
--   (1) 신호 도달률을 사후 집계할 수 있다(감마플립이 실제로 몇 분에나 산출됐는가).
--   (2) "왜 그 판단이었나"를 재구성할 수 있다 — 지금은 risk_gate_state 요약만 있어 불가능하다.
--
--   gamma_flip                    그 분의 Gamma Flip 레벨. NULL = 산출 실패(정상적으로도 탐색
--                                 범위 밖일 수 있으므로 NULL 자체가 곧 오류는 아니다).
--   gex                           그 분의 GEX. 부호가 v6 §11.4 프리미엄 매도 게이트의 입력이다.
--   chain_leg_count               판단에 쓴 체인 스냅샷의 레그 수. 예상값은 북 수 x (ATM±N)x2.
--   chain_oldest_leg_age_seconds  스냅샷에서 가장 오래된 레그의 나이. 08-03에는 이 값이
--                                 사실상 4주(2,400,000초)였는데 아무도 몰랐다.
--
-- 전부 NULL 허용이다 — 체인이 비었거나 스팟이 없으면 판단은 계속하되 이 값들만 비워 둔다
-- (없는 값을 0으로 채우면 "계산했는데 0"과 구분되지 않는다).

ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS gamma_flip DOUBLE PRECISION;
ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS gex DOUBLE PRECISION;
ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS chain_leg_count INTEGER;
ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS chain_oldest_leg_age_seconds DOUBLE PRECISION;

COMMENT ON COLUMN signal_decisions.gamma_flip IS
    '판단 시점의 Gamma Flip 레벨(mahdi.features.options_intel.find_gamma_flip). NULL은 산출 실패이며, '
    '탐색 범위(스팟±5%) 안에 부호 전환이 없는 정상적인 경우도 포함한다 — 산출률이 낮게 유지되면 '
    '행사가 창(ATM±N)이 좁거나 스팟에서 벗어나 있는지 본다(2026-08-03 §2-1/§2-2).';
COMMENT ON COLUMN signal_decisions.gex IS
    '판단 시점의 GEX. 부호가 곧 "딜러가 변동성을 억제(+)하는가 증폭(-)하는가"이며 v6 §11.4 '
    '프리미엄 매도 게이트(positive GEX 요구)의 입력이다.';
COMMENT ON COLUMN signal_decisions.chain_leg_count IS
    '판단에 쓴 체인 스냅샷의 레그 수. 예상값은 (폴링 중인 북 수) x (ATM±N 행사가) x 2(C/P) — '
    '크게 벗어나면 db.latest_option_chain()의 신선도/만기 경계를 의심한다(2026-08-03 §2-2: '
    '경계가 없어 246레그가 반환됐고 그중 오늘 수집분은 10개였다).';
COMMENT ON COLUMN signal_decisions.chain_oldest_leg_age_seconds IS
    '체인 스냅샷에서 가장 오래된 레그의 나이(초). db.CHAIN_SNAPSHOT_MAX_AGE_MINUTES 상한 안에 '
    '머물러야 한다 — 2026-08-03에는 이 값이 사실상 4주였는데 관측 지표가 없어 아무도 몰랐다.';
