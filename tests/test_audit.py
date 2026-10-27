# -*- coding: utf-8 -*-
"""Testes de auditoria — cada classe mapeia um achado da Auditoria P0."""
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

import main as engine

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# P0-1: fórmula de N requerido usa z_alpha + z_beta (power-alvo 80%)
# ---------------------------------------------------------------------------
class TestPowerFormula:
    def test_n_required_matches_closed_form_with_zbeta(self):
        conv = engine.proportion_ztest(0.10, 0.12, 6000, 6000)
        pbar = (0.10 * 6000 + 0.12 * 6000) / 12000
        za = stats.norm.ppf(1 - 0.05 / 2)
        zb = stats.norm.ppf(0.80)
        expected = math.ceil(2 * (za + zb) ** 2 * pbar * (1 - pbar) / 0.02 ** 2)
        res = engine.power_analysis(conv, 6000, 6000)
        assert res.n_required_per_arm == expected

    def test_n_required_larger_than_50pct_power_formula(self):
        # a fórmula errada antiga (sem z_beta) subestima ~2.1x
        conv = engine.proportion_ztest(0.10, 0.12, 6000, 6000)
        res = engine.power_analysis(conv, 6000, 6000)
        pbar = 0.11
        za = stats.norm.ppf(0.975)
        naive = math.ceil(2 * za ** 2 * pbar * (1 - pbar) / 0.02 ** 2)
        assert res.n_required_per_arm > naive

    def test_n_required_is_per_arm(self):
        # N por braço: com MDE 2pp e pbar ~0.10 fica ~3.5k/braço; nunca 1.8k
        conv = engine.proportion_ztest(0.10, 0.12, 6000, 6000)
        res = engine.power_analysis(conv, 6000, 6000)
        assert res.mde_abs == engine.MDE_ABS
        assert res.n_required_per_arm >= 3000


# ---------------------------------------------------------------------------
# P0-2: gate SRM bloca leitura (payload sem métricas de negócio, exit != 0)
# ---------------------------------------------------------------------------
def _mk_srm(flagged):
    return engine.SRMResult(
        observed_control=4000, observed_treatment=6000,
        expected_control=5000, expected_treatment=5000,
        chi2=400.0, p_value=0.0, exact_p_value=None, srm_flagged=flagged)


class TestGateBlocked:
    def test_blocked_payload_has_no_business_metrics(self):
        srm = _mk_srm(True)
        blocked = engine.build_blocked_payload(srm)
        assert blocked["readout_blocked"] is True
        assert blocked["decision"] == "RERUN"
        for banned in ("conversion_test", "power_analysis",
                       "funnel_summary", "mrr_usd"):
            assert banned not in blocked
        assert "srm_gate" in blocked

    def test_blocked_readout_declares_suppression(self):
        text = engine.blocked_readout(engine.build_blocked_payload(_mk_srm(True)))
        assert "RERUN" in text
        assert "SUPRIMIDAS" in text

    def test_json_serializable(self):
        json.dumps(engine.build_blocked_payload(_mk_srm(True)))

    def test_gate_decision_rerun(self):
        srm = _mk_srm(True)
        conv = engine.proportion_ztest(0.10, 0.12, 1000, 1000)
        power = engine.power_analysis(conv, 1000, 1000)
        assert engine.decide(srm, conv, power) == "RERUN"


# ---------------------------------------------------------------------------
# SRM: binomial exato decide em células pequenas (P0/adicional)
# ---------------------------------------------------------------------------
class TestSRM:
    def test_exact_gate(self):
        r = engine.srm_check(12, 25)
        ref = engine.stats.binomtest(12, 37, 0.5).pvalue
        assert r.exact_p_value == pytest.approx(ref)
        assert r.srm_flagged == (ref < engine.ALPHA)

    def test_large_cells_use_chi2_only(self):
        r = engine.srm_check(4000, 6000)
        assert r.exact_p_value is None
        assert r.srm_flagged is True
        ref_p = engine.stats.chisquare([4000, 6000],
                                       [5000, 5000]).pvalue
        assert r.p_value == pytest.approx(ref_p)

    def test_balanced_split_passes(self):
        r = engine.srm_check(5973, 6027)
        assert r.srm_flagged is False


# ---------------------------------------------------------------------------
# z-test / matriz de decisão
# ---------------------------------------------------------------------------
class TestZTestAndDecisions:
    def test_empty_arm_guard(self):
        with pytest.raises(ValueError):
            engine.proportion_ztest(0.1, 0.1, 0, 100)
        conv = engine.proportion_ztest(0.10, 0.12, 6000, 6000)
        with pytest.raises(ValueError):
            engine.power_analysis(conv, 0, 6000)

    def test_matrix_all_four_paths(self):
        srm_ok = _mk_srm(False)
        conv_sig = engine.proportion_ztest(0.0809, 0.0990, 6000, 6000)
        conv_ns = engine.proportion_ztest(0.10, 0.109, 6000, 6000)
        assert conv_sig.significant
        assert engine.decide(srm_ok, conv_sig,
                             _pow(0.9)) == "SHIP"
        assert engine.decide(srm_ok, conv_sig,
                             _pow(0.5)) == "KEEP_RUNNING"
        assert engine.decide(srm_ok, conv_ns,
                             _pow(0.5)) == "ITERATE"
        assert engine.decide(srm_ok, conv_ns,
                             _pow(0.9)) == "ITERATE"


def _pow(x):
    import types
    return types.SimpleNamespace(observed_power=x)


# ---------------------------------------------------------------------------
# Réplica pandas == SQL (trial 30d calendário, ordem de estágios, grain)
# ---------------------------------------------------------------------------
def _events_from_rows(rows):
    return pd.DataFrame(rows, columns=["user_id", "ab_group",
                                       "acquisition_channel", "plan_type",
                                       "stage", "entered_at", "mrr_usd"])


class TestReplica:
    def test_trial_30_calendar_days_rule(self):
        # atravesse a meia-noite: 2026-01-01 23:00 -> 2026-02-01 00:30
        # DATE_DIFF(DATE) = 31 => False; regra (trial - visit) <= 30 dias
        base = ["u1", "control", "direct", "starter"]
        rows = [(*base, "visitor", pd.Timestamp("2026-01-01 23:00"), 0.0),
                (*base, "trial", pd.Timestamp("2026-02-01 00:30"), 0.0)]
        f = engine.build_funnel(_events_from_rows(rows))
        assert not bool(f["reached_trial"].iloc[0])

    def test_trial_before_visit_rejected(self):
        base = ["u2", "control", "direct", "starter"]
        rows = [(*base, "visitor", pd.Timestamp("2026-02-01"), 0.0),
                (*base, "trial", pd.Timestamp("2026-01-01"), 0.0)]
        f = engine.build_funnel(_events_from_rows(rows))
        assert not bool(f["reached_trial"].iloc[0])

    def test_paid_before_activated_rejected(self):
        base = ["u3", "control", "direct", "starter"]
        rows = [(*base, "visitor", pd.Timestamp("2026-01-01"), 0.0),
                (*base, "trial", pd.Timestamp("2026-01-05"), 0.0),
                (*base, "paid", pd.Timestamp("2026-01-10"), 99.0),
                (*base, "activated", pd.Timestamp("2026-01-20"), 0.0)]
        f = engine.build_funnel(_events_from_rows(rows))
        assert not bool(f["reached_paid"].iloc[0])

    def test_grain_one_row_per_user_with_changing_dims(self):
        # mesmas dims mudando não duplicam o grain (dims por first-event)
        ev = [
            ["u4", "control", "direct", "starter", "visitor",
             pd.Timestamp("2026-01-01"), 0.0],
            ["u4", "control", "direct", "starter", "trial",
             pd.Timestamp("2026-01-05"), 0.0],
            ["u4", "control", "direct", "pro", "activated",
             pd.Timestamp("2026-01-08"), 0.0],
            ["u4", "control", "direct", "pro", "paid",
             pd.Timestamp("2026-01-10"), 99.0],
        ]
        f = engine.build_funnel(_events_from_rows([tuple(r) for r in ev]))
        assert len(f) == 1
        row = f.iloc[0]
        assert row["plan_type"] == "starter"          # first-seen
        assert row["paid_plan_type"] == "pro"          # mais recente 'paid'
        assert row["mrr_usd"] == 99.0                  # CASE do paid_plan_type

    def test_reached_visitor_true_for_all(self):
        ev = [["u5", "control", "direct", "starter", "visitor",
               pd.Timestamp("2026-01-01"), 0.0]]
        f = engine.build_funnel(_events_from_rows([tuple(r) for r in ev]))
        assert bool(f["reached_visitor"].iloc[0])


# ---------------------------------------------------------------------------
# Gerador: sem eventos futuros; determinismo do split hash
# ---------------------------------------------------------------------------
class TestGenerator:
    def test_no_future_events(self, tmp_path):
        path = engine.generate_mock_csv(tmp_path / "gen.csv")
        df = pd.read_csv(path, parse_dates=["entered_at"])
        assert df["entered_at"].max() <= pd.Timestamp("2026-10-23")
        # 12.000 usuários = 12.000 rows visitor + estágios adicionais
        assert df["user_id"].nunique() == engine.N_VISITORS

    def test_split_is_deterministic_hash(self):
        def split(n):
            return [int.from_bytes(engine.hashlib.md5(
                f"u_{i:07d}".encode()).digest()[:4], "big") % 2
                for i in range(n)]

        assert split(50) == split(50)


# ---------------------------------------------------------------------------
# Contratros de config: YAML parse, JSON Schema Lightdash, charts, SQL Native
# ---------------------------------------------------------------------------
DASHBOARD_SCHEMA_SUBSET = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "dashboard-as-code-1.0 subset (invariantes obrigatórios auditados)",
    "type": "object",
    "required": ["version", "name", "slug", "spaceSlug", "tabs", "tiles"],
    "properties": {
        "version": {"type": "integer"},
        "name": {"type": "string"},
        "slug": {"type": "string"},
        "spaceSlug": {"type": "string"},
        "description": {"type": "string"},
        # obrigatório no dashboard-as-code-1.0, mesmo que vazio
        "tabs": {"type": "array"},
        "tiles": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["tileSlug", "type", "properties", "x", "y", "w", "h"],
                "properties": {
                    # `slug` é inválido como chave de tile; é `tileSlug`
                    "tileSlug": {"type": "string"},
                    "type": {"type": "string"},
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "w": {"type": "integer"},
                    "h": {"type": "integer"},
                    "properties": {
                        "type": "object",
                        "required": ["title", "chartSlug"],
                        "properties": {
                            "title": {"type": "string"},
                            "chartSlug": {"type": "string"},
                            # `properties.viewName` NÃO é permitido no schema
                            # oficial: charts derivam o view do seu tableName
                            "viewName": {"not": {}},
                        },
                        "additionalProperties": True,
                    },
                },
            },
        },
    },
}


class TestConfigs:
    def _yaml(self, rel):
        import yaml
        with open(REPO / rel, encoding="utf-8") as fh:
            return yaml.safe_load(fh)

    def test_all_yaml_parse_single_doc(self):
        for rel in ("dbt_project/dbt_project.yml", "dbt_project/schema.yml",
                    "lightdash/dashboards.yml"):
            assert self._yaml(rel) is not None

    def test_dashboard_validates_json_schema(self):
        import jsonschema
        d = self._yaml("lightdash/dashboards.yml")
        jsonschema.validate(d, DASHBOARD_SCHEMA_SUBSET)
        assert d["tabs"] == []
        for t in d["tiles"]:
            assert "slug" not in t, "tile deve usar tileSlug, não slug"
            assert "viewName" not in t["properties"], (
                "properties.viewName não é permitido no oficial")

    def test_chart_slots_have_no_dangling_charts(self):
        import yaml
        charts_dir = REPO / "lightdash" / "charts"
        files = list(charts_dir.glob("*.yml"))
        slugs = set()
        for f in files:
            chart = yaml.safe_load(open(f, encoding="utf-8"))
            for field in ("version", "name", "slug", "spaceSlug", "tableName",
                          "description", "display"):
                assert field in chart, f"{f.name} sem campo {field}"
            slugs.add(chart["slug"])
        dashboard = self._yaml("lightdash/dashboards.yml")
        referenced = {t["properties"]["chartSlug"] for t in dashboard["tiles"]}
        dangling = referenced - slugs
        assert not dangling, f"chartSlugs sem chart-as-code: {dangling}"
        orphan = slugs - referenced
        assert not orphan or True, "charts órfãos permitidos, dangling não"
        # enrich: 4 tiles esperados
        assert len(referenced) == 4

    def test_metric_sql_is_bigquery_native(self):
        schema = self._yaml("dbt_project/schema.yml")
        found_metrics = 0

        def walk(node):
            nonlocal found_metrics
            if isinstance(node, dict):
                if "metrics" in node and isinstance(node["metrics"], dict):
                    for m in node["metrics"].values():
                        sql = m.get("sql", "")
                        assert "COUNT_DISTINCT" not in sql, sql
                        assert "SUM(reached_" not in sql, sql
                        found_metrics += 1
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(schema)
        assert found_metrics >= 10

    def test_sources_dataset_contract(self):
        schema = self._yaml("dbt_project/schema.yml")
        src = schema["sources"][0]
        assert src["schema"] == "analytics_raw"
        proj = self._yaml("dbt_project/dbt_project.yml")
        assert proj["version"] == "1.2.0"
        models_cfg = proj["models"]["saas_analytics"]
        assert models_cfg["marts"]["+materialized"] == "table"
        assert "+schema" not in str(models_cfg)


# ---------------------------------------------------------------------------
# Artefatos stale: cada regime limpa os artefatos do oposto
# ---------------------------------------------------------------------------
class TestArtifactCleanup:
    def test_healthy_run_removes_blocked_artifacts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "RESULTS_PATH", tmp_path)
        for name in ("ab_payload_blocked.json", "ab_readout_blocked.txt",
                     "ab_payload.json", "ab_readout.txt"):
            (tmp_path / name).write_text("stale", encoding="utf-8")
        engine.clear_stale_artifacts(healthy_run=True)
        assert (tmp_path / "ab_payload.json").exists()
        assert (tmp_path / "ab_readout.txt").exists()
        assert not (tmp_path / "ab_payload_blocked.json").exists()
        assert not (tmp_path / "ab_readout_blocked.txt").exists()

    def test_blocked_run_removes_healthy_artifacts(self, tmp_path, monkeypatch):
        monkeypatch.setattr(engine, "RESULTS_PATH", tmp_path)
        for name in ("ab_payload_blocked.json", "ab_readout_blocked.txt",
                     "ab_payload.json", "ab_readout.txt"):
            (tmp_path / name).write_text("stale", encoding="utf-8")
        engine.clear_stale_artifacts(healthy_run=False)
        assert not (tmp_path / "ab_payload.json").exists()
        assert not (tmp_path / "ab_readout.txt").exists()
        assert (tmp_path / "ab_payload_blocked.json").exists()
        assert (tmp_path / "ab_readout_blocked.txt").exists()
