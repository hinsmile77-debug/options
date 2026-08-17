-- Mahdi 추가 (2026-08-17, v6 §11.5 Instrument Selection) — 판단이 고른 **종목**을 남긴다.
--
-- ## 이 컬럼이 없어서 생긴 빈칸
--
-- §11.4의 출력은 전략 **이름**(`atm_long`, `debit_spread`, …)이고 Execution의 입력은
-- **단축상품번호**(`EntryContext.symbol`)다. 그 사이를 잇는 계층이 코드에도 스펙에도 없었고,
-- `signal_decisions`에는 "어느 전략이 허용됐나"까지만 있고 "그래서 무엇을 살 것인가"가 없었다.
-- 유일한 호출부인 백테스트는 `symbol="BACKTEST"` 자리표시자를 넣고 있었다. 라이브가 미배선이라
-- 아무도 이 빈칸을 밟지 않았을 뿐이다.
--
-- ## 왜 근거까지 JSONB로 함께 넣는가
--
-- 선택기는 델타·OI·거래량·스프레드·만기 잔존일을 **계산해서 고른다.** 고른 결과만 남기고 그
-- 입력을 버리면 나중에 «무엇이 이 행사가를 골랐나»에 답할 수 없다 — `member_scores`를 뒤늦게
-- 살린 것과 정확히 같은 이유다(2026-08-05 고도화#4). `risk_gate_state`가 이미 같은 방식으로
-- 판단 근거를 싣고 있으므로 형태를 맞춘다.
--
-- ## 사유가 값보다 중요하다
--
-- 이 컬럼은 후보가 **없을 때도** 채워진다(`{"candidates": [], "reason": "..."}`). 「후보가
-- 없었다」와 「선택기가 안 돌았다」가 구분되지 않으면 이 기록은 쓸모가 없다(규약 C — NULL은
-- 오직 «선택기가 그 사이클에 안 돌았다»만 뜻한다). 사유 코드는
-- `mahdi/fusion/instrument_selection.py`의 `REASON_*` 상수가 유일한 출처다.
--
-- ## 임계값을 지금 안 정한다
--
-- 유동성 하한(`oi` / `spread_state` / 당일 체결 유무)은 `strategy_params.yaml`에 자리만 만들고
-- **값은 비워 둔다.** 모의투자 실측 분포를 보기 전에 임계를 정하면 그 임계가 곧 결론이 된다
-- (2026-08-05 스팟 괴리율에서 한 실수). 필터가 전부 꺼진 상태가 이 마이그레이션 시점의 설계다.

ALTER TABLE signal_decisions ADD COLUMN IF NOT EXISTS selected_instruments JSONB;

COMMENT ON COLUMN signal_decisions.selected_instruments IS
    'v6 §11.5 종목 선택기 출력. {candidates:[{strategy, legs:[{symbol, expiry, strike, option_type, '
    'side, rule, delta, oi, volume, spread_state}]}], book_expiry, reason, rejected:[...]}. '
    'NULL = 그 사이클에 선택기가 돌지 않았다(진입 후보가 아니었거나 배선 이전 행). '
    'candidates=[] + reason 있음 = 돌았으나 고를 것이 없었다 — 둘은 다른 사건이다. '
    'reason 코드의 출처: mahdi/fusion/instrument_selection.py REASON_*.';
