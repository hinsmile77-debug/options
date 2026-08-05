"""Regime 패널 — 현재 레짐 배지 + 8-state 확률 막대 (v6 §17 COCKPIT, PART 7).

색상은 Okabe-Ito CVD-safe 팔레트를 RegimeLabel 고정 순서에 매핑한다(카테고리 색상은
순위가 아니라 정체성을 따라야 하므로, 확률 크기와 무관하게 레짐마다 항상 같은 색).
"""

from __future__ import annotations

import plotly.graph_objects as go

from mahdi.engines.regime import RegimeLabel

_REGIME_COLORS: dict[RegimeLabel, str] = {
    RegimeLabel.TREND_UP_STRONG: "#0072B2",
    RegimeLabel.TREND_DOWN_STRONG: "#D55E00",
    RegimeLabel.RANGE_BALANCED: "#009E73",
    RegimeLabel.RANGE_BREAK_PREP: "#E69F00",
    RegimeLabel.VOL_EXPANSION: "#CC79A7",
    RegimeLabel.VOL_COMPRESSION: "#56B4E9",
    RegimeLabel.LIQUIDITY_THIN: "#F0E442",
    RegimeLabel.CRISIS_DEFENSE: "#000000",
}

REGIME_LABEL_KO: dict[RegimeLabel, str] = {
    RegimeLabel.TREND_UP_STRONG: "강한 상승 추세",
    RegimeLabel.TREND_DOWN_STRONG: "강한 하락 추세",
    RegimeLabel.RANGE_BALANCED: "평균회귀",
    RegimeLabel.RANGE_BREAK_PREP: "압축·돌파대기",
    RegimeLabel.VOL_EXPANSION: "변동성 팽창",
    RegimeLabel.VOL_COMPRESSION: "변동성 압축",
    RegimeLabel.LIQUIDITY_THIN: "유동성 빈약",
    RegimeLabel.CRISIS_DEFENSE: "위기·이벤트",
}


_WARMUP_TITLE = "⚠ WARMUP 폴백 — 학습된 확률이 아니라 전일 마감 레짐을 그대로 쓴 값입니다(엔진 미학습)"
_TRAINED_TITLE = "학습된 레짐 엔진의 사후확률"
_UNKNOWN_TITLE = "학습/WARMUP 구분 불가 — 마이그레이션 025 이전에 적재된 행입니다"


def _warmup_title(is_warmup: bool | None) -> str:
    if is_warmup is None:
        return _UNKNOWN_TITLE
    return _WARMUP_TITLE if is_warmup else _TRAINED_TITLE


def build_regime_probability_chart(
    regime_prob: dict[RegimeLabel, float], is_warmup: bool | None = None
) -> go.Figure:
    """
    입력: 레짐별 확률, `is_warmup`(True면 이 값은 확률이 아니라 `warmup_fallback()`의 one-hot 상수).
    계산: 8개 레짐의 현재 확률을 가로 막대로 표시 (레짐당 고정 색상, 범례 불필요 — 축 라벨이
         정체성을 대신함). warmup이면 제목에 그 사실을 못박는다.
    해석: 2026-08-05(COCKPIT 육안 점검 P1-7). 08-05 화면은 "평균회귀 100%, 나머지 0%"였는데,
         학습된 HMM 사후확률로 읽으면 **8개 중 하나를 100% 확신**한다는 뜻이다. 실제로는
         `RegimeEngine.fit()`이 한 번도 실행된 적이 없어(feature_store 6,830/8,000행)
         `warmup_fallback()`이 전일 마감 레짐에 1.0을 박아 넣은 상수였다 — 즉 **"확신"이 아니라
         "확률을 계산한 적이 없다"**. 같은 그림에 다른 뜻이 두 개 있으면 화면은 거짓말을 한다.
         `is_warmup=None`(마이그레이션 025 이전 행)은 **모른다**고 쓴다 — 학습된 판정으로
         가정하는 쪽이 더 위험하다.
    """
    labels = list(RegimeLabel)
    values = [regime_prob.get(r, 0.0) for r in labels]
    colors = [_REGIME_COLORS[r] for r in labels]
    names = [REGIME_LABEL_KO[r] for r in labels]

    fig = go.Figure(
        go.Bar(
            x=values,
            y=names,
            orientation="h",
            marker_color=colors,
            text=[f"{v:.0%}" for v in values],
            textposition="outside",
            # 2026-08-05(P1-7): x=1.0인 막대의 바깥 라벨이 축 밖으로 잘려나가, 08-05 화면에서
            # **유일하게 의미 있던 100% 막대만 값이 안 보였다.** 축을 조금 늘리고 클리핑을 끈다.
            cliponaxis=False,
            hovertemplate="%{y}: %{x:.1%}<extra></extra>",
        )
    )
    fig.update_layout(
        title=dict(text=_warmup_title(is_warmup), font=dict(size=12), x=0, xanchor="left"),
        xaxis=dict(title="확률", range=[0, 1.08], tickformat=".0%", tickvals=[0, 0.2, 0.4, 0.6, 0.8, 1.0]),
        yaxis=dict(title=None, autorange="reversed"),
        showlegend=False,
        margin=dict(l=10, r=10, t=36, b=10),
        height=280,
    )
    return fig
