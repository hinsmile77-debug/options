-- Mahdi Phase 1 추가 — 현재 사용 중인 선물 단축코드 레지스트리(분기마다 바뀜).
-- 대시보드가 "이 종목이 선물인지 옵션인지"를 vpin 유무 같은 휴리스틱으로 추측하지 않고
-- 바로 조회할 수 있게 한다. 시계열이 아니라 단일 현재값 레지스트리라 하이퍼테이블로 안 만든다.

CREATE TABLE IF NOT EXISTS active_futures_symbol (
    underlying VARCHAR(20) PRIMARY KEY,
    symbol VARCHAR(20) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL);
