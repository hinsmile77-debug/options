-- Mahdi Phase 1 추가 (2026-07-10) — 사용자가 KIS 앱/HTS에서 해외선물옵션 CBOT 거래소 신청을
-- 완료해 CME/CBOT 상장 상품(ZN, 10년 국채선물) 조회가 열렸다. 기존 us10y_yield(해외주식
-- 국채구분 I, 일봉)는 실제 수익률(%) 레벨을 그대로 유지하고, zn_front(ZN 선물 근월물 현재가)를
-- 별도 컬럼으로 추가해 5분 주기 "급변" 감지에 쓴다 — 선물가는 수익률과 역상관이라 단위가
-- 다르므로 같은 컬럼에 섞지 않는다(가격 급락 = 수익률 급등 스트레스 신호로 해석).

ALTER TABLE macro_snapshot_5m ADD COLUMN IF NOT EXISTS zn_front DECIMAL(12,4);
