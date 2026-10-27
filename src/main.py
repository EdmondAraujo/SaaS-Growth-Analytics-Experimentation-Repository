#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
src/main.py — Statistical engine + AI readout for the SaaS growth A/B test.

Pipeline (ordem de execução é o contrato de integridade):
  1. Gera/carrega o espelho local do warehouse (data/ab_test_mock.csv),
     réplica da tabela analytics_raw.funnel_events (ver dbt/schema.yml).
  2. GATE de SRM (Sample Ratio Mismatch): chi-quadrado sobre a alocação
     visitor -> paid por grupo; em células pequenas (<=30), o teste
     binomial exato bicaudal decide o gate. A leitura de negócio é
     BLOQUEADA se o gate falhar: nenhuma métrica de conversão/power/MRR é
     computada e o processo sai com exit code 2 (contrato de integridade).
  3. Teste de conversão: z-test de duas proporções (SE pooled, bicaudal)
     sobre visitor -> paid, com IC de 95% (Wald, SE unpooled).
  4. Power post-hoc + N POR BRAÇO necessário para detectar o MDE (2pp)
     com power-alvo 80%: n = 2*(z_alpha + z_beta)^2 * p_bar(1-p_bar)/MDE^2.
  5. Payload estruturado -> Claude API (executive readout) ou
     dry-run heurístico local quando ANTHROPIC_API_KEY não está definida.
     O readout é chaveado por vetor de decisão; em caso de falha da API,
     cai para o heurístico local. A estatística é 100% determinística aqui.

Estatística usada:
  - z = (p_t - p_c) / sqrt(p_bar(1-p_bar)(1/n_c + 1/n_t)), p_bar pooled
  - p-value = 2 * (1 - Phi(|z|))
  - power = Phi( (|delta| - z_crit*SE0) / SE1 )  com SE1 unpooled
  - N por braço p/ MDE = 2*(z_alpha + z_beta)^2 * p_bar(1-p_bar)/MDE^2,
    com z_alpha = PPF(1 - alpha/2) e z_beta = PPF(TARGET_POWER). Aquela
    forma antiga sem z_beta (só z_crit) correspondia a power ~50% — proibida.
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import hashlib

import numpy as np
from scipy import stats

try:
    import anthropic
except ImportError:  # dry-run roda até sem a lib opcional
    anthropic = None

# ---------------------------------------------------------------------------
# CONFIG — parâmetros de referência (alpha/power/MDE) travados no código
# ---------------------------------------------------------------------------
EXPERIMENT_NAME = "activation_flow_v3"
ALPHA = 0.05                    # bicaudal
TARGET_POWER = 0.80
MDE_ABS = 0.02                  # 2 pp em conversão visitor -> paid
SRM_EXPECTED_SPLIT = (0.5, 0.5)  # 50/50 declarado no desenho do experimento
SRM_MIN_CELL_EXACT = 30         # abaixo disso, teste binomial exato decide o gate
N_VISITORS = 12_000
SCALE_PAID = {"starter": 29.0, "pro": 99.0, "enterprise": 399.0}

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = REPO_ROOT / "data" / "ab_test_mock.csv"
RESULTS_PATH = REPO_ROOT / "src" / "results"

RNG_SEED = 20261023  # reproducibilidade: seed fixa versionada no git

# ground truth do mock (desconhecido ao analista, herdado do desenho)
# Encadeamento condicional do funil: 40% trial, 85% ativam o trial,
# 30% (control) / 34.2% (treatment) dos ativados pagam.
# => visitor->paid ~10.2% (control) vs ~11.7% (treatment): lift absoluto
#    ~+1.46pp, relativo ~+14% — rente ao MDE de 2pp, desenhado para pressure
#    o readout entre SHIP e KEEP_RUNNING.
_TRUE_PAID_RATE_CONTROL = 0.30     # condicional activated -> paid
_TRUE_PAID_RATE_TREATMENT = 0.342
_TRUE_TRIAL_RATE = 0.40            # visitor -> trial
_TRUE_ACTIVATED_RATE = 0.85        # trial -> activated

# ---------------------------------------------------------------------------
# Camada 1 — espelho do warehouse (mock BigQuery)
# ---------------------------------------------------------------------------
STAGES = ("visitor", "trial", "activated", "paid")
CHANNELS = ("organic_search", "paid_search", "referral", "social", "direct")
PLANS = ("starter", "pro", "enterprise")


def generate_mock_csv(path: Path, n_visitors: int = N_VISITORS,
                      seed: int = RNG_SEED) -> Path:
    """Gera o espelho local do CSV réplicando analytics_raw.funnel_events."""
    uid = np.array([f"u_{i:07d}" for i in range(n_visitors)])
    # alocação REAL do desenho: hash determinístico do user_id (50/50,
    # reprodutível entre runs — mesmo comportamento do assignment service)
    hashed = np.array(
        [int.from_bytes(hashlib.md5(u.encode()).digest()[:4], "big") % 2
         for u in uid]
    )
    groups = np.where(hashed == 0, "control", "treatment")

    rng = np.random.default_rng(seed)
    channel = rng.choice(CHANNELS, size=n_visitors,
                         p=[0.35, 0.25, 0.15, 0.10, 0.15])
    plan = rng.choice(PLANS, size=n_visitors, p=[0.55, 0.35, 0.10])

    days = rng.integers(0, 90, size=n_visitors)          # 90 dias de janela
    secs = rng.integers(0, 86_400, size=n_visitors)
    base_ts = np.datetime64("2026-04-01T00:00:00")  # janela passada:_nunca futuro
    visits_ts = base_ts + days.astype("timedelta64[D]") \
                + secs.astype("timedelta64[s]")

    rate_paid = np.where(groups == "control",
                         _TRUE_PAID_RATE_CONTROL, _TRUE_PAID_RATE_TREATMENT)
    reaches_trial = rng.random(n_visitors) < _TRUE_TRIAL_RATE
    reaches_activated = reaches_trial & (rng.random(n_visitors) < _TRUE_ACTIVATED_RATE)
    reaches_paid = reaches_activated & (rng.random(n_visitors) < rate_paid)

    mrr = np.where(
        reaches_paid,
        np.vectorize(SCALE_PAID.__getitem__)(plan),
        0.0,
    )

    rows = []
    for i in range(n_visitors):
        visit_ts = visits_ts[i]
        # timestamps monotônicos por estágio (contrato saas_funnel.sql)
        trial_ts = visit_ts + np.timedelta64(int(rng.integers(0, 8)) * 86_400)
        activated_ts = trial_ts + np.timedelta64(int(rng.integers(2, 21)) * 86_400)
        paid_ts = activated_ts + np.timedelta64(int(rng.integers(1, 11)) * 86_400)
        rows.append((uid[i], groups[i], channel[i], plan[i], "visitor",
                     _ts(visit_ts), 0.0))
        if reaches_trial[i]:
            rows.append((uid[i], groups[i], channel[i], plan[i], "trial",
                         _ts(trial_ts), 0.0))
        if reaches_activated[i]:
            rows.append((uid[i], groups[i], channel[i], plan[i], "activated",
                         _ts(activated_ts), 0.0))
        if reaches_paid[i]:
            rows.append((uid[i], groups[i], channel[i], plan[i], "paid",
                         _ts(paid_ts), float(mrr[i])))

    import csv
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["user_id", "ab_group", "acquisition_channel",
                    "plan_type", "stage", "entered_at", "mrr_usd"])
        w.writerows(rows)
    return path


def _ts(x) -> str:
    return np.datetime_as_string(x, unit="s")


def load_funnel_events(path: Path):
    """Carrega o espelho do warehouse (long format, 1 linha por (user, stage))."""
    import pandas as pd
    if not path.exists():
        generate_mock_csv(path)
    df = pd.read_csv(path, parse_dates=["entered_at"])
    return df


# ---------------------------------------------------------------------------
# Camada 2 — construção do funil user-level (réplica de saas_funnel.sql)
# ---------------------------------------------------------------------------
def build_funnel(events) -> "pd.DataFrame":
    """Réplica em pandas do modelo dbt saas_funnel.

    Contrato 1:1 com a SQL BigQuery (staging/marts/saas_funnel.sql):
      - grain user_id (dimensões por first-event, ordem de chegada)
      - reached_trial: trial>=first_seen E DATE_DIFF(DATE) <= 30 (dias inteiros!)
      - reached_activated: trial_started_at not null E activated_at >= trial
      - reached_paid: activated e trial preenchidos E paid_at >= activated_at
      - mrr_usd derivado do paid_plan_type (CASE no SQL)
    """
    import pandas as pd

    conf = events.pivot_table(
        index="user_id",
        columns="stage",
        values="entered_at",
        aggfunc="min",
    ).reset_index()

    for s in STAGES:
        if s not in conf.columns:
            conf[s] = pd.NaT
    conf = conf.rename(columns={
        "visitor": "first_seen_at",
        "trial": "trial_started_at",
        "activated": "activated_at",
        "paid": "paid_at",
    })

    # dims por ordem de chegada (ARRAY_AGG ... ORDER BY entered_at LIMIT 1):
    # drop_duplicates(keep='first') equivale ao SQL — pega a PRIMEIRA linha
    # inclusive com valor nulo; groupby.first() ignoraria nulos e divergia.
    order = events.sort_values(["user_id", "entered_at"], kind="stable")
    first_dims = order.drop_duplicates(subset="user_id", keep="first")[
        ["user_id", "ab_group", "acquisition_channel", "plan_type"]]
    conf = conf.merge(first_dims, on="user_id", how="left")

    # paid_plan_type: plan_type do evento 'paid' mais recente (IGNORE NULLS, DESC)
    paid_events = events[events["stage"] == "paid"] \
        .sort_values(["user_id", "entered_at"]) \
        .groupby("user_id")["plan_type"].last()
    conf["paid_plan_type"] = conf["user_id"].map(paid_events)

    conf["reached_visitor"] = conf["first_seen_at"].notna()
    conf["reached_trial"] = (
        conf["trial_started_at"].notna()
        & conf["first_seen_at"].notna()
        & (conf["trial_started_at"] >= conf["first_seen_at"])
        # DATE_DIFF(DATE(a), DATE(b), DAY) = dias CALENDÁRIO inteiros:
        # os fuso-horários/horas não entram na regra (floor em 'D')
        & ((conf["trial_started_at"].dt.floor("D")
            - conf["first_seen_at"].dt.floor("D")).dt.days <= 30)
    )
    conf["reached_activated"] = (
        conf["activated_at"].notna()
        & conf["trial_started_at"].notna()
        & (conf["activated_at"] >= conf["trial_started_at"])
    )
    conf["reached_paid"] = (
        conf["paid_at"].notna()
        & (conf["trial_started_at"].notna())
        & (conf["activated_at"].notna())
        & (conf["paid_at"] >= conf["activated_at"])
    )

    # mrr_usd derivado de paid_plan_type (idêntico ao CASE do SQL)
    conf["mrr_usd"] = conf["paid_plan_type"].map(SCALE_PAID)
    return conf


# ---------------------------------------------------------------------------
# Camada 3 — estatística
# ---------------------------------------------------------------------------
@dataclass
class ArmStats:
    group: str
    visitors: int
    trials: int
    activated: int
    paid: int
    conversion: float          # visitor -> paid
    trial_to_paid: float
    activated_to_paid: float
    mrr_usd: float


@dataclass
class SRMResult:
    observed_control: int
    observed_treatment: int
    expected_control: int
    expected_treatment: int
    chi2: float
    p_value: float
    exact_p_value: Optional[float]
    srm_flagged: bool


@dataclass
class ConversionResult:
    metric: str
    control_rate: float
    treatment_rate: float
    abs_lift: float
    rel_lift: float
    z_stat: float
    p_value: float
    ci_low: float
    ci_high: float
    significant: bool


@dataclass
class PowerResult:
    alpha: float
    observed_power: float
    power_at_mde: float
    n_required_per_arm: int
    mde_abs: float
    n_per_arm_current: float


def summarize_arm(funnel, group: str) -> ArmStats:
    g = funnel[funnel["ab_group"] == group]
    visitors = len(g)
    trials = int(g["reached_trial"].sum())
    activated = int(g["reached_activated"].sum())
    paid = int(g["reached_paid"].sum())
    return ArmStats(
        group=group,
        visitors=visitors,
        trials=trials,
        activated=activated,
        paid=paid,
        conversion=(paid / visitors) if visitors else 0.0,
        trial_to_paid=(paid / trials) if trials else 0.0,
        activated_to_paid=(paid / activated) if activated else 0.0,
        mrr_usd=float(g["mrr_usd"].fillna(0).sum()),
    )


def srm_check(sc: int, st: int) -> SRMResult:
    """Gate de integridade do split. SRM é testado ANTES de qualquer leitura.

    Passo primário: chi-quadrado aderência 50/50 (df=1).
    Robustez em células pequenas: teste binomial exato bicaudal (p=0.5),
    que é a alternativa exata correta para dois grupos — Fisher exato
    exigiria margens condicionais que 50/50 esperado não preserva.
    """
    exp_c = (sc + st) * SRM_EXPECTED_SPLIT[0]
    exp_t = (sc + st) * SRM_EXPECTED_SPLIT[1]
    chi2, p = stats.chisquare(f_obs=[sc, st], f_exp=[exp_c, exp_t])
    exact_p = None
    if min(sc, st) <= SRM_MIN_CELL_EXACT:
        exact_p = float(stats.binomtest(sc, sc + st, 0.5).pvalue)
    # células pequenas: o teste exato É o gate (chi2 é assintótico)
    if exact_p is not None:
        flagged = exact_p < ALPHA
    else:
        flagged = p < ALPHA
    return SRMResult(
        observed_control=sc,
        observed_treatment=st,
        expected_control=int(round(exp_c)),
        expected_treatment=int(round(exp_t)),
        chi2=float(chi2),
        p_value=float(p),
        exact_p_value=exact_p,
        srm_flagged=bool(flagged),
    )


def proportion_ztest(pc: float, pt: float, nc: int, nt: int) -> ConversionResult:
    """z-test bicaudal de duas proporções (SE pooled) + IC de 95% (Wald)."""
    if nc <= 0 or nt <= 0:
        raise ValueError("braço vazio: teste de conversão indeterminado")
    delta = pt - pc
    p_bar = (pc * nc + pt * nt) / (nc + nt)
    se0 = math.sqrt(p_bar * (1 - p_bar) * (1 / nc + 1 / nt))
    z = delta / se0 if se0 > 0 else 0.0
    p_val = 2.0 * (1.0 - stats.norm.cdf(abs(z)))
    se1 = math.sqrt(pc * (1 - pc) / nc + pt * (1 - pt) / nt)
    z_crit = stats.norm.ppf(1 - ALPHA / 2)
    return ConversionResult(
        metric="visitor_to_paid",
        control_rate=pc,
        treatment_rate=pt,
        abs_lift=delta,
        rel_lift=(delta / pc) if pc > 0 else 0.0,
        z_stat=float(z),
        p_value=float(p_val),
        ci_low=delta - z_crit * se1,
        ci_high=delta + z_crit * se1,
        significant=bool(p_val < ALPHA),
    )


def power_analysis(res: ConversionResult, nc: int, nt: int) -> PowerResult:
    """Power post-hoc (observado) e power/N no MDE de referência.

    N requerido POR BRAÇO (power-alvo TARGET_POWER) usa a fórmula do
    desenho clássico de duas proporções:
        n = 2 * (z_alpha + z_beta)^2 * p_bar(1-p_bar) / MDE^2
    com z_alpha = PPF(1-alpha/2) e z_beta = PPF(TARGET_POWER).
    """
    if nc <= 0 or nt <= 0:
        raise ValueError("braço vazio: power analysis indeterminada")
    p_c, p_t = res.control_rate, res.treatment_rate
    delta = abs(res.abs_lift)
    p_bar = (p_c * nc + p_t * nt) / (nc + nt)
    se0 = math.sqrt(p_bar * (1 - p_bar) * (1 / nc + 1 / nt))
    se1 = math.sqrt(p_c * (1 - p_c) / nc + p_t * (1 - p_t) / nt)
    z_crit = stats.norm.ppf(1 - ALPHA / 2)
    z_beta = stats.norm.ppf(TARGET_POWER)

    obs_power = (
        stats.norm.cdf((delta - z_crit * se0) / se1)
        + stats.norm.cdf((-delta - z_crit * se0) / se1)
        if delta > 0 and se1 > 0 else 0.0
    )

    power_mde = (
        stats.norm.cdf((MDE_ABS - z_crit * se0) / se1)
        + stats.norm.cdf((-MDE_ABS - z_crit * se0) / se1)
        if se1 > 0 else 0.0
    )

    n_req = (2.0 * (z_crit + z_beta) ** 2 * p_bar * (1 - p_bar)) / (MDE_ABS ** 2) \
        if p_bar > 0 else math.inf
    return PowerResult(
        alpha=ALPHA,
        observed_power=float(obs_power),
        power_at_mde=float(power_mde),
        n_required_per_arm=int(math.ceil(n_req)) if math.isfinite(n_req) else -1,
        mde_abs=MDE_ABS,
        n_per_arm_current=(nc + nt) / 2,
    )


def decide(srm: SRMResult, conv: ConversionResult,
           power: PowerResult) -> str:
    """Matriz de decisão executiva (mesma ordem do contracto auditável)."""
    if srm.srm_flagged:
        return "RERUN"          # integridade do tráfego primeiro
    if conv.significant and power.observed_power >= TARGET_POWER:
        return "SHIP"
    if conv.significant and power.observed_power < TARGET_POWER:
        return "KEEP_RUNNING"   # significância inicial + power insuficiente
    return "ITERATE"


# ---------------------------------------------------------------------------
# Camada 4 — payload + Claude API / dry-run
# ---------------------------------------------------------------------------
def build_payload(srm, conv, power, stored: list, c_mrr: float, t_mrr: float) -> dict:
    return {
        "experiment": EXPERIMENT_NAME,
        "config": {
            "alpha": ALPHA,
            "two_sided": True,
            "target_power": TARGET_POWER,
            "srm_expected_split": list(SRM_EXPECTED_SPLIT),
            "mde_abs_pp": MDE_ABS * 100,
        },
        "srm_gate": {
            "method": "chi_square_1df (binomial exact fallback small cells)",
            **asdict(srm),
        },
        "conversion_test": {**asdict(conv), "metric": "visitor_to_paid"},
        "power_analysis": asdict(power),
        "funnel_summary": {
            arm.group: {
                "visitors": arm.visitors,
                "trials": arm.trials,
                "activated": arm.activated,
                "paid": arm.paid,
                "trial_to_paid_rate": round(arm.trial_to_paid, 4),
                "mrr_usd": round(arm.mrr_usd, 2),
            }
            for arm in stored
        },
        "mrr_usd": {"control": round(c_mrr, 2), "treatment": round(t_mrr, 2)},
    }


SYSTEM_PROMPT = (
    "Você é um Growth Lead de uma SaaS B2B. Receberá um payload JSON com "
    "resultados estatísticos de um teste A/B (gate de SRM, z-test de "
    "conversão e power analysis). Escreva um executive readout em <= 250 "
    "palavras, com: (1) veredito em UMA linha (SHIP / KEEP_RUNNING / "
    "ITERATE / RERUN); (2) os 3 números-chave que justificam; (3) risco "
    "residual e próximo passo operacional. Nunca recalcule a estatística; "
    "confie no payload e interprete."
)


def claude_readout(payload: dict) -> str:
    """Chama a Claude API com fallback garantido ao heurístico local.

    Sem ANTHROPIC_API_KEY, ou em qualquer falha de API/rede/resposta,
    o readout heurístico local é emitido — o pipeline nunca quebra por
    causa da camada de narração.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key and anthropic is not None:
        try:
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=700,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user",
                           "content": "Payload:\n"
                                      + json.dumps(payload, indent=2,
                                                   ensure_ascii=False)}],
            )
            return msg.content[0].text
        except Exception as exc:  # API/parse/rede: nunca bloquear o operador
            print(f"[warn] Claude API falhou ({type(exc).__name__}); "
                  "usando readout heurístico local.")
    return heuristic_readout(payload)


def blocked_readout(payload: dict) -> str:
    """Readout executivo quando o gate de SRM bloqueia a leitura."""
    srm = payload["srm_gate"]
    lines = [
        "[READOUT BLOQUEADO — gate de SRM falhou]",
        f"Experimento: {payload['experiment']}",
        "",
        "VEREDITO: RERUN",
        "",
        "Números-chave:",
        f" • SRM gate: chi2={srm['chi2']:.3f}, p={srm['p_value']:.4f}, "
        f"observed split {srm['observed_control']} vs "
        f"{srm['observed_treatment']} (esperado ~{srm['expected_control']})",
        " • Métricas de negócio SUPRIMIDAS por integridade: nenhum número de"
        " conversão/power/MRR é confiável sobre tráfego desbalanceado.",
        "",
        "Próximo passo:",
        " • Auditar assignment log + pipeline de ingestão antes de qualquer "
        "decisão de negócio; re-executar somente com o split íntegro.",
    ]
    return "\n".join(lines)


def heuristic_readout(payload: dict) -> str:
    """Dry-run: readout heurístico idêntico à matriz de decisão computada aqui."""
    srm = payload["srm_gate"]
    conv = payload["conversion_test"]
    power = payload["power_analysis"]
    decision = decide(
        SRMResult(**{k: v for k, v in srm.items()
                     if k in SRMResult.__dataclass_fields__}),
        ConversionResult(**conv),
        PowerResult(**power),
    )
    lines = [
        f"[DRY-RUN READOUT — sem ANTHROPIC_API_KEY; heurística local]",
        f"Experimento: {payload['experiment']}",
        "",
        f"VEREDITO: {decision}",
        "",
        "Números-chave:",
        f" • SRM gate: chi2={srm['chi2']:.3f}, p={srm['p_value']:.4f} "
        f"({'FALHOU' if srm['srm_flagged'] else 'APROVADO — split íntegro'})",
        f" • Conversão visitor->paid: control {conv['control_rate']*100:.2f}% "
        f"vs treatment {conv['treatment_rate']*100:.2f}% "
        f"(lift absoluto {conv['abs_lift']*100:+.2f} pp, "
        f"relativo {conv['rel_lift']*100:+.1f}%)",
        f" • z={conv['z_stat']:.3f}, p={conv['p_value']:.4f} "
        f"{'(significativo a 5%)' if conv['significant'] else '(não significativo a 5%)'}",
        f" • IC 95% do lift: [{conv['ci_low']*100:+.2f} pp, {conv['ci_high']*100:+.2f} pp]",
        f" • Power observado: {power['observed_power']:.2%} "
        f"(alvo {payload['config']['target_power']:.0%}; N por braço hoje = "
        f"{power['n_per_arm_current']:.0f}, necessário p/ MDE "
        f"{payload['config']['mde_abs_pp']:.0f}pp = "
        f"{power['n_required_per_arm']})",
        "",
        "Próximo passo:",
    ]
    if decision == "SHIP":
        lines.append(" • Derrubar o experimento para 100% do tráfego e "
                     "monitorar MRR por 14 dias antes de rotular holdout.")
    elif decision == "KEEP_RUNNING":
        lines.append(" • Manter coleta até N>=n_required_per_arm por braço; "
                     "não interromper a experiência em meio ao cycle.")
    elif decision == "RERUN":
        lines.append(" • PAUSAR leituras de negócio, auditar o assignment log "
                     "e o SRM antes de qualquer decisão de negócio.")
    else:
        lines.append(" • Iterar a variação: lift observado não sustenta "
                     "decisão com este tamanho de amostra; priorizar o MDE no design.")
    return "\n".join(lines)


def build_blocked_payload(srm) -> dict:
    """Payload de gate bloqueado: METRICAS DE NEGOCIO SUPRIMIDAS.

    Contrato de integridade: sobre tráfego com SRM, nenhum número de
    conversão/power/MRR é emitido — apenas o diagnóstico do gate.
    """
    return {
        "experiment": EXPERIMENT_NAME,
        "readout_blocked": True,
        "decision": "RERUN",
        "config": {
            "alpha": ALPHA,
            "two_sided": True,
            "target_power": TARGET_POWER,
            "srm_expected_split": list(SRM_EXPECTED_SPLIT),
            "mde_abs_pp": MDE_ABS * 100,
        },
        "srm_gate": {
            "method": "chi_square_1df (binomial exact fallback small cells)",
            **asdict(srm),
        },
    }


def clear_stale_artifacts(healthy_run: bool) -> None:
    """Remove artefatos do modo oposto: rodada bloqueada apaga os artefatos
    saudáveis e vice-versa. Garante que o consumidor do nome padrão de
    arquivo não leia um resultado desatualizado de outro regime do gate.
    """
    RESULTS_PATH.mkdir(parents=True, exist_ok=True)
    stale = (
        ("ab_payload_blocked.json", "ab_readout_blocked.txt")
        if healthy_run
        else ("ab_payload.json", "ab_readout.txt")
    )
    for name in stale:
        try:
            (RESULTS_PATH / name).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Orquestração
# ---------------------------------------------------------------------------
def main() -> int:
    """Pipeline executora. Exit codes: 0 = readout emitido; 2 = SRM gate."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"== {EXPERIMENT_NAME} — stat engine ==  (data: {DATA_PATH.name})")
    events = load_funnel_events(DATA_PATH)
    funnel = build_funnel(events)
    RESULTS_PATH.mkdir(parents=True, exist_ok=True)

    # Nível 1 — alocação por braço (valida guards antes de estatística)
    ctl = summarize_arm(funnel, "control")
    trt = summarize_arm(funnel, "treatment")
    if ctl.visitors == 0 or trt.visitors == 0:
        print("ERRO: braço vazio detectado; impossível validar integridade.")
        return 3

    # Nível 2 — GATE de SRM: roda E decide ANTES de qualquer leitura de negócio
    srm = srm_check(ctl.visitors, trt.visitors)
    print(f"[1/4] SRM gate: {srm.observed_control} vs {srm.observed_treatment} "
          f"(esperado ~{srm.expected_control}) "
          f"-> chi2={srm.chi2:.4f}, p={srm.p_value:.4f}, "
          f"{'FALHOU' if srm.srm_flagged else 'ok'}")
    if srm.exact_p_value is not None:
        print(f"      Binomial exact (gate em células pequenas): "
              f"p={srm.exact_p_value:.4f}")

    if srm.srm_flagged:
        # BLOQUEIO: payload só carrega diagnóstico; conversão/power/MRR
        # NÃO são computados nem emitidos (contrato de integridade).
        clear_stale_artifacts(healthy_run=False)
        blocked = build_blocked_payload(srm)
        blocked_path = RESULTS_PATH / "ab_payload_blocked.json"
        with open(blocked_path, "w", encoding="utf-8") as f:
            json.dump(blocked, f, indent=2, ensure_ascii=False)
        readout = blocked_readout(blocked)
        (RESULTS_PATH / "ab_readout_blocked.txt").write_text(readout,
                                                            encoding="utf-8")
        print(f"      >>> GATE BLOQUEADO: métricas de negócio suprimidas; "
              f"payload de diagnóstico em {blocked_path.name}")
        print("\n--- EXECUTIVE READOUT (BLOQUEADO) ---")
        print(readout)
        return 2

    # Nível 3 — teste de conversão (somente com split íntegro)
    conv = proportion_ztest(ctl.conversion, trt.conversion,
                            ctl.visitors, trt.visitors)
    print(f"[2/4] z-test visitor->paid: z={conv.z_stat:.4f}, "
          f"p={conv.p_value:.5f}, lift={conv.abs_lift*100:+.2f}pp "
          f"IC95% [{conv.ci_low*100:+.2f}, {conv.ci_high*100:+.2f}]")

    power = power_analysis(conv, ctl.visitors, trt.visitors)
    print(f"[3/4] power: observado={power.observed_power:.3f}, "
          f"@MDE={MDE_ABS*100:.0f}pp={power.power_at_mde:.3f}, "
          f"N por braço necessário (power {TARGET_POWER:.0%})="
          f"{power.n_required_per_arm}")

    clear_stale_artifacts(healthy_run=True)
    payload = build_payload(srm, conv, power, [ctl, trt], ctl.mrr_usd,
                            trt.mrr_usd)
    payload_path = RESULTS_PATH / "ab_payload.json"
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)

    readout = claude_readout(payload)
    (RESULTS_PATH / "ab_readout.txt").write_text(readout, encoding="utf-8")
    print(f"[4/4] payload salvo em {payload_path}")

    print("\n--- EXECUTIVE READOUT ---")
    print(readout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
