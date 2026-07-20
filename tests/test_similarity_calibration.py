"""Phase 1a similarity-gate synthetic calibration (ROADMAP 1a: "a one-off
distribution sanity check against ~50 known pairs", proposal §6.3).

No Ollama on this machine — a REAL bge-m3 calibration cannot run here. This
suite is its synthetic equivalent plus the reproducible rig for the real one:

- A fixed corpus of 60 hand-written CJK finance topic pairs in three known
  tiers (20 each): paraphrase (same question reworded → the gate should skip),
  related (same theme, different question → mostly augment), unrelated
  (different themes → pass).
- A deterministic proxy embedder: character n-grams (CJK uni+bigrams, whole
  ASCII words) hashed into a fixed-dim bag vector + cosine. It is a
  STRUCTURAL stand-in for bge-m3 — it preserves ordering/separation between
  the tiers, not the real model's absolute cosine scale. Hashing uses
  zlib.crc32 (stable across processes; builtin hash() is seed-randomized).
- The whiteboard gate's classifier (`whiteboard._classify_prior`) is run over
  the full similarity × board-age matrix and the verdict distribution is
  asserted per tier and printed as a table into the test log.

Because the proxy scale ≠ bge-m3 scale, the classifier matrix here uses
PROXY_* thresholds calibrated against this corpus's measured distribution —
the production defaults 0.85/0.65 (proposal values, see
whiteboard.SIMILARITY_DEFAULTS) occupy the same STRUCTURAL position on the
real model's scale: skip cuts between the paraphrase and related bands,
augment cuts between the related and unrelated bands.

Real calibration path (once Ollama + bge-m3 are up — same corpus, same
assertions, real embedder):

    INSTITUTE_ENABLE_VECTORS=1 INSTITUTE_CALIBRATION_REAL=1 \\
        .venv/bin/python -m pytest tests/test_similarity_calibration.py -q -rP

`test_real_bge_m3_calibration` then prints the same distribution table under
the production 0.85/0.65 thresholds plus suggested cut points; tune the
admin_state row via PUT /api/whiteboard/similarity-config from that output.
"""
from __future__ import annotations

import math
import os
import re
import statistics
import zlib

import pytest

from app.institute import whiteboard

# ---- corpus: 60 known pairs, three tiers, CJK finance ------------------------

PARAPHRASE_PAIRS = [  # same question reworded → expected verdict: skip
    ("美联储9月议息会议降息25个基点的概率", "联储九月FOMC会议降息25bp的可能性"),
    ("英伟达2026财年四季度业绩指引能否超预期", "英伟达FY2026 Q4业绩指引超预期的概率"),
    ("A股半导体设备国产化替代的投资机会", "A股半导体设备国产替代带来的投资机会"),
    ("人民币兑美元汇率破7的风险", "人民币对美元汇率跌破7的风险有多大"),
    ("港股互联网平台公司的估值修复空间", "港股互联网平台企业估值修复的空间"),
    ("中国10年期国债收益率下行空间", "中国十年期国债收益率还有多少下行空间"),
    ("白酒行业春节动销数据对全年业绩的指引", "白酒春节动销对全年业绩的指引意义"),
    ("创新药企业出海BD交易的可持续性", "创新药出海BD交易能否持续"),
    ("光伏产业链价格战何时见底", "光伏产业链的价格战什么时候见底"),
    ("存储芯片涨价周期的持续性分析", "存储芯片本轮涨价周期能持续多久"),
    ("日本央行加息对日元套息交易的冲击", "日央行加息对日元套息交易的影响"),
    ("房地产收储政策对去库存的实际效果", "地产收储政策去库存的实际效果评估"),
    ("黄金价格创新高后的配置价值", "金价创历史新高之后的配置价值"),
    ("AI算力需求对电力基础设施的拉动", "AI算力需求拉动电力基础设施投资"),
    ("中概股回港二次上市的进展与影响", "中概股回香港二次上市的进展及其影响"),
    ("新能源车渗透率见顶的讨论", "新能源汽车渗透率是否见顶"),
    ("美国对华芯片出口管制升级的影响", "美国升级对华芯片出口管制的影响分析"),
    ("银行净息差收窄压力与分红可持续性", "银行业净息差收窄压力下分红能否持续"),
    ("量化基金超额收益衰减的原因", "量化基金超额收益为何衰减"),
    ("碳酸锂价格反弹的供需基础", "碳酸锂价格本轮反弹的供需支撑"),
]

RELATED_PAIRS = [  # same theme, different question → expected: mostly augment
    ("英伟达四季度业绩指引超预期的概率", "AMD MI400系列对英伟达市场份额的冲击"),
    ("美联储降息路径的市场定价", "美国十年期国债收益率的下行空间"),
    ("白酒行业春节动销数据", "白酒行业库存去化进度与批价走势"),
    ("光伏产业链价格战何时见底", "光伏组件出口欧洲的需求恢复"),
    ("创新药出海BD交易的可持续性", "创新药医保谈判降价对企业盈利的影响"),
    ("半导体设备国产化替代进展", "半导体先进制程扩产对设备订单的拉动"),
    ("人民币兑美元汇率走势展望", "人民币升值下出口企业的汇兑损益压力"),
    ("港股互联网平台的估值修复", "港股互联网平台公司回购与分红的力度"),
    ("存储芯片涨价周期的持续性", "存储厂商HBM高带宽内存的供需缺口"),
    ("日本央行加息节奏的判断", "日本央行政策转向对日元汇率的影响"),
    ("房地产收储政策的去库存效果", "房地产企业债务重组的进展与化债路径"),
    ("黄金创新高后的配置价值", "全球央行购金趋势对黄金的长期支撑"),
    ("AI算力需求对电力设施的拉动", "AI数据中心液冷渗透率提升的受益链"),
    ("新能源车渗透率是否见顶", "新能源车动力电池产能过剩与出清节奏"),
    ("银行净息差收窄的压力", "银行涉房贷款敞口的资产质量压力"),
    ("量化基金超额收益衰减", "小盘股流动性对量化策略容量的约束"),
    ("碳酸锂价格反弹的供需基础", "碳酸锂上游锂矿成本曲线与减产幅度"),
    ("美国对华芯片出口管制升级", "芯片管制下国产GPU厂商的替代窗口期"),
    ("中概股回港二次上市的进展", "港股流动性改善与南向资金流入的持续性"),
    ("美股AI板块估值是否过高", "美股科技巨头AI资本开支指引的变化"),
]

UNRELATED_PAIRS = [  # different themes → expected verdict: pass
    ("英伟达四季度业绩指引", "白酒行业春节动销数据"),
    ("美联储降息路径的市场定价", "创新药出海BD交易的可持续性"),
    ("光伏产业链价格战何时见底", "港股互联网平台的估值修复"),
    ("人民币汇率破7的风险", "存储芯片涨价周期的持续性"),
    ("日本央行加息节奏的判断", "A股半导体设备国产化替代"),
    ("房地产收储政策的效果", "量化基金超额收益衰减的原因"),
    ("黄金创新高后的配置价值", "新能源车渗透率见顶的讨论"),
    ("AI算力需求对电力设施的拉动", "银行净息差收窄的压力"),
    ("碳酸锂价格反弹的供需基础", "中概股回港二次上市的进展"),
    ("美国对华芯片出口管制升级", "白酒行业库存去化与批价走势"),
    ("中国十年期国债收益率下行空间", "光伏组件出口欧洲的需求恢复"),
    ("创新药医保谈判降价的影响", "日元套息交易平仓的冲击范围"),
    ("银行涉房贷款敞口的资产质量", "HBM高带宽内存的供需缺口"),
    ("小盘股流动性与量化策略容量", "全球央行购金趋势的持续性"),
    ("房地产企业债务重组化债路径", "AI数据中心液冷渗透率提升"),
    ("动力电池产能过剩与出清节奏", "港股南向资金流入的持续性"),
    ("半导体先进制程扩产的拉动", "白酒批价走势与渠道信心"),
    ("美股科技巨头资本开支指引", "碳酸锂上游锂矿成本曲线"),
    ("互联网平台回购与分红力度", "美国十年期国债收益率走势"),
    ("国产GPU厂商的替代窗口期", "黄金创历史新高后的配置思路"),
]

TIERS: dict[str, list[tuple[str, str]]] = {
    "paraphrase": PARAPHRASE_PAIRS,
    "related": RELATED_PAIRS,
    "unrelated": UNRELATED_PAIRS,
}

# ---- deterministic proxy embedder (structural stand-in for bge-m3) -----------

PROXY_DIM = 4096
_TOKEN = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")


def _ngrams(text: str) -> list[str]:
    grams: list[str] = []
    for tok in _TOKEN.findall(text.casefold()):
        if tok[0].isascii():
            grams.append(tok)  # whole ASCII words: fomc / bp / hbm / ai …
            continue
        grams.extend(tok)  # CJK unigrams
        grams.extend(tok[i:i + 2] for i in range(len(tok) - 1))  # CJK bigrams
    return grams


def proxy_embed(text: str) -> list[float]:
    vec = [0.0] * PROXY_DIM
    for gram in _ngrams(text):
        vec[zlib.crc32(gram.encode("utf-8")) % PROXY_DIM] += 1.0
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return 0.0 if na == 0.0 or nb == 0.0 else dot / (na * nb)


# PROXY-scale thresholds, calibrated against this corpus's measured proxy
# distribution (see the printed table): the skip cut sits between the related
# band's max (~0.51) and the paraphrase band's min (~0.53); the augment cut
# between the unrelated max (~0.14) and the related p25 (~0.19). They mirror
# the STRUCTURAL role of the production 0.85/0.65 on the bge-m3 scale — the
# numbers themselves are not comparable across embedders.
PROXY_SKIP_THRESHOLD = 0.52
PROXY_AUGMENT_THRESHOLD = 0.16


def _tier_sims(embedder=proxy_embed) -> dict[str, list[float]]:
    return {
        tier: sorted(_cosine(embedder(a), embedder(b)) for a, b in pairs)
        for tier, pairs in TIERS.items()
    }


def _summary(sims: list[float]) -> dict[str, float]:
    n = len(sims)
    return {
        "min": sims[0], "p25": sims[n // 4], "med": statistics.median(sims),
        "p75": sims[3 * n // 4], "max": sims[-1],
    }


def _classify_all(
    sims: dict[str, list[float]], skip_t: float, augment_t: float, board_age_days: float,
) -> dict[str, dict[str, int]]:
    """Run the REAL gate classifier over every pair at one board age."""
    cfg = {
        **whiteboard.SIMILARITY_DEFAULTS,
        "skip_threshold": skip_t, "augment_threshold": augment_t,
    }
    skip_cutoff = whiteboard._iso_ago(days=cfg["skip_window_days"])
    augment_cutoff = whiteboard._iso_ago(days=cfg["augment_window_days"])
    created_at = whiteboard._iso_ago(days=board_age_days)
    out: dict[str, dict[str, int]] = {}
    for tier, values in sims.items():
        counts = {"skip": 0, "augment": 0, "pass": 0}
        for sim in values:
            verdict = whiteboard._classify_prior(
                sim, created_at, cfg,
                skip_cutoff=skip_cutoff, augment_cutoff=augment_cutoff,
            )
            counts[verdict] += 1
        out[tier] = counts
    return out


def _print_table(
    title: str, sims: dict[str, list[float]], verdicts: dict[str, dict[str, int]],
    skip_t: float, augment_t: float,
) -> None:
    print(f"\n{title}")
    print(f"thresholds: skip>={skip_t:g}  augment>={augment_t:g}  (recent board, both windows open)")
    print(f"{'tier':<11} {'n':>3} {'min':>6} {'p25':>6} {'med':>6} {'p75':>6} {'max':>6} | {'skip':>4} {'augm':>4} {'pass':>4}")
    for tier, values in sims.items():
        s, v = _summary(values), verdicts[tier]
        print(
            f"{tier:<11} {len(values):>3} {s['min']:>6.3f} {s['p25']:>6.3f} {s['med']:>6.3f} "
            f"{s['p75']:>6.3f} {s['max']:>6.3f} | {v['skip']:>4} {v['augment']:>4} {v['pass']:>4}"
        )


# ---- corpus shape ------------------------------------------------------------

def test_corpus_has_50_plus_known_pairs():
    """The ROADMAP acceptance unit: 50+ pairs of KNOWN relationship."""
    assert sum(len(p) for p in TIERS.values()) >= 50
    for tier, pairs in TIERS.items():
        assert len(pairs) == 20, tier
        assert len(set(pairs)) == 20, f"duplicate pair in {tier}"
        for a, b in pairs:
            assert a.strip() and b.strip() and a != b


def test_proxy_embedder_is_deterministic():
    text = "美联储9月议息会议降息25个基点的概率"
    assert proxy_embed(text) == proxy_embed(text)
    assert any(proxy_embed(text))  # non-degenerate


# ---- tier separation (the actual sanity: known pairs land in distinct bands) --

def test_tier_distributions_separate():
    sims = _tier_sims()
    para, rel, unrel = (_summary(sims[t]) for t in ("paraphrase", "related", "unrelated"))
    # paraphrase band sits clearly above the related band …
    assert para["p25"] > rel["p75"], (para, rel)
    assert para["min"] > rel["med"], (para, rel)
    # … and the related band clearly above the unrelated band
    assert rel["p75"] > unrel["max"], (rel, unrel)
    assert rel["med"] > unrel["p75"], (rel, unrel)


# ---- the classifier matrix over the real gate code ---------------------------

def test_classify_prior_matrix_recent_boards():
    """Recent prior board (inside both windows): the three tiers must land in
    their expected verdict bands under the proxy-scale thresholds. Prints the
    distribution table (visible via pytest -rP / -s)."""
    sims = _tier_sims()
    verdicts = _classify_all(sims, PROXY_SKIP_THRESHOLD, PROXY_AUGMENT_THRESHOLD, board_age_days=3)
    _print_table(
        "similarity-gate synthetic calibration — proxy embedder (char n-gram), 60 CJK finance pairs",
        sims, verdicts, PROXY_SKIP_THRESHOLD, PROXY_AUGMENT_THRESHOLD,
    )
    n = 20
    para, rel, unrel = verdicts["paraphrase"], verdicts["related"], verdicts["unrelated"]
    # paraphrase pairs: the gate should overwhelmingly skip (measured: 20/20)
    assert para["skip"] >= 0.85 * n, para
    # related pairs: never strong enough to skip; mostly augment (measured: 15/20 augment, 0 skip)
    assert rel["skip"] == 0, rel
    assert rel["augment"] >= 0.6 * n, rel
    # unrelated pairs: overwhelmingly pass (measured: 20/20)
    assert unrel["pass"] >= 0.95 * n, unrel


def test_classify_prior_matrix_stale_boards_all_pass():
    """The time axis of the matrix: a prior board outside BOTH windows must
    pass regardless of similarity — even an identical topic."""
    sims = _tier_sims()
    stale_age = max(
        whiteboard.SIMILARITY_DEFAULTS["skip_window_days"],
        whiteboard.SIMILARITY_DEFAULTS["augment_window_days"],
    ) + 15
    verdicts = _classify_all(
        sims, PROXY_SKIP_THRESHOLD, PROXY_AUGMENT_THRESHOLD, board_age_days=stale_age,
    )
    for tier, counts in verdicts.items():
        assert counts == {"skip": 0, "augment": 0, "pass": 20}, (tier, counts)


def test_classify_prior_matrix_mid_age_boards_skip_decays_to_augment():
    """Between the windows (skip 14d < age <= augment 30d) a paraphrase-grade
    prior may no longer skip, only augment — the gate's intended decay."""
    sims = _tier_sims()
    verdicts = _classify_all(sims, PROXY_SKIP_THRESHOLD, PROXY_AUGMENT_THRESHOLD, board_age_days=20)
    assert verdicts["paraphrase"]["skip"] == 0
    assert verdicts["paraphrase"]["augment"] == 20
    assert verdicts["unrelated"]["pass"] >= 19


# ---- real bge-m3 calibration (opt-in; the rig the synthetic run stands in for) -

@pytest.mark.skipif(
    os.environ.get("INSTITUTE_CALIBRATION_REAL") != "1",
    reason="real bge-m3 calibration; needs Ollama — set INSTITUTE_CALIBRATION_REAL=1",
)
async def test_real_bge_m3_calibration(monkeypatch):
    """Same corpus, real embedder, PRODUCTION thresholds (0.85/0.65): prints
    the measured distribution + suggested cut points so a human can tune the
    admin_state config row. Only the tier separation is hard-asserted — the
    verdict split IS the thing being calibrated."""
    from app.institute import vectors

    monkeypatch.setattr(vectors, "_enabled", lambda: True)
    monkeypatch.setattr(vectors, "_ollama_down_until", 0.0)
    monkeypatch.setattr(vectors, "_ollama_warned", False)

    cache: dict[str, list[float]] = {}

    async def emb(text: str) -> list[float]:
        if text not in cache:
            vec = await vectors.embed(text)
            if vec is None:
                pytest.skip("Ollama unreachable or embed model missing")
            cache[text] = vec
        return cache[text]

    sims: dict[str, list[float]] = {}
    for tier, pairs in TIERS.items():
        sims[tier] = sorted([_cosine(await emb(a), await emb(b)) for a, b in pairs])

    skip_t = whiteboard.SIMILARITY_DEFAULTS["skip_threshold"]
    augment_t = whiteboard.SIMILARITY_DEFAULTS["augment_threshold"]
    verdicts = _classify_all(sims, skip_t, augment_t, board_age_days=3)
    _print_table(
        f"similarity-gate REAL calibration — {vectors.model_name()}, 60 CJK finance pairs",
        sims, verdicts, skip_t, augment_t,
    )
    para, rel, unrel = (_summary(sims[t]) for t in ("paraphrase", "related", "unrelated"))
    print(
        "suggested cut points from this run: "
        f"skip ≈ {(rel['max'] + para['p25']) / 2:.3f} "
        f"(between related.max {rel['max']:.3f} and paraphrase.p25 {para['p25']:.3f}), "
        f"augment ≈ {(unrel['max'] + rel['p25']) / 2:.3f} "
        f"(between unrelated.max {unrel['max']:.3f} and related.p25 {rel['p25']:.3f}); "
        "apply via PUT /api/whiteboard/similarity-config"
    )
    assert para["p25"] > rel["p75"], (para, rel)
    assert rel["med"] > unrel["p75"], (rel, unrel)
