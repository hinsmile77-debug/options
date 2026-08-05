-- Mahdi 추가 (2026-08-05, COCKPIT 육안 점검 P1-7) — 이 레짐이 학습된 판정인가, WARMUP 폴백인가.
--
-- **왜 필요한가**: 08-05 12:12 COCKPIT의 Regime 패널은 "평균회귀 100%, 나머지 7개 0%"를
-- 그리고 있었다. 학습된 HMM의 사후확률로 읽으면 **8개 레짐 중 하나를 100% 확신**한다는 뜻이지만,
-- 실제로는 `RegimeEngine.fit()`이 아직 한 번도 실행된 적이 없어(feature_store 6,830/8,000행)
-- `regime.warmup_fallback()`이 낸 one-hot 상수였다 — 그 함수는 갭 z-score 임계에 안 걸리면
-- **전일 마감 레짐을 그대로** 돌려주고 `prob_vector[regime] = 1.0`으로 채운다.
--
-- 즉 화면의 100%는 "확신"이 아니라 "확률을 계산한 적이 없다"는 뜻이었다.
--
-- **왜 stability_flag로는 대신할 수 없는가**: `warmup_fallback()`은 stability_flag=False를 내고
-- `predict()`도 max(prob) < 임계면 False를 낸다 — **두 경우가 같은 값으로 합쳐진다.** 08-05에
-- 종일 0%(0/207행)였던 그 지표로는 "엔진이 미학습이라 못 재는 것"과 "재봤더니 불안정한 것"이
-- 구분되지 않았고, COCKPIT은 후자로 읽히는 화면(REGIME_UNSTABLE)을 그리고 있었다.
--
-- `RegimeState.is_warmup` 필드는 2026-07-10부터 코드에 있었지만 **DB에 저장된 적이 없어**
-- (코드베이스 전체에서 mahdi/engines/regime.py 밖 참조 0건) 소비할 방법 자체가 없었다.
-- 이번 점검에서 반복해 나온 형태 — 배선은 돼 있고 데이터가 그것을 못 쓰게 만든다 — 와 같다.
--
--   is_warmup  TRUE  = warmup_fallback()이 낸 값(prob_vector는 one-hot 상수, 확률이 아니다)
--              FALSE = RegimeEngine.predict()가 낸 학습된 사후확률
--              NULL  = 이 마이그레이션 이전에 적재된 행(둘 중 무엇인지 알 수 없다 —
--                      0으로 채우면 "학습된 판정"이라고 거짓말하는 것이라 NULL로 남긴다)

ALTER TABLE regime_state ADD COLUMN IF NOT EXISTS is_warmup BOOLEAN;

COMMENT ON COLUMN regime_state.is_warmup IS
    'TRUE면 이 행의 prob_vector는 warmup_fallback()의 one-hot 상수이지 학습된 사후확률이 아니다 '
    '(2026-08-05 COCKPIT 육안 점검 P1-7). COCKPIT Regime 패널이 이 값으로 확률 막대 대신 '
    '"엔진 미학습" 배너를 띄운다 — 08-05에 "평균회귀 100%"가 확신으로 읽히던 화면의 정정. '
    'stability_flag로 대신할 수 없다: 그 값은 미학습(warmup)과 학습됐지만 불안정한 경우를 '
    '같은 False로 합친다. NULL은 이 마이그레이션 이전 행(구분 불가) — 0으로 채우지 않는다.';
