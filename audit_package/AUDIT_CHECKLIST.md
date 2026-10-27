# AUDIT CHECKLIST — saas_analytics v1.1.0 (pós-correções P0)
Snapshot executável fornecido pelo Coder. O Auditor deve validar e emitir
veredito. **CSV de evidência incluído** (data/ab_test_mock.csv, 22.208 rows,
12.000 users, sha256 990a7f449b4604dc... — ver AUDIT_REPORT.md).

## 1. P0 resolvidos (re-verificar)
- [ ] **Power/N:** n_required_per_arm usa 2·(z_α+z_β)²·p̄(1−p̄)/MDE² (n=3799/braço @ 80%); TARGET_POWER entra na fórmula
- [ ] **Gate SRM bloqueia:** main() sai com exit 2; payload blocked SEM conversion/power/MRR; artifacts ab_payload_blocked.json + ab_readout_blocked.txt
- [ ] **Lightdash:** dashboards.yml em documento único; tiles com slug/type/properties.chartSlug/x/y/w/h; slug+spaceSlug no top level (sem dimensiones/metrics duplicadas — em dbt meta)
- [ ] **BigQuery Native:** COUNT(DISTINCT)/COUNTIF em todas as meta-metrics (zero COUNT_DISTINCT, zero SUM(bool)); CAC via fct_channel_spend (join mês+canal)
- [ ] **Evidência:** AUDIT_REPORT com números da run atual + SHA256 do CSV elétrico; tests/ inclusos no pacote

## 2. Adicionais resolvidos
- [ ] dbt configs aplicáveis (models/Staging + Marts em subpastas, materializations efetivas)
- [ ] Regra de trial 1:1 pandas==SQL (DATE_DIFF em dias calendário + rejection trial<visit)
- [ ] Réplica completa (reached_visitor, paid_plan_type, mrr por CASE em paid_plan_type)
- [ ] Grain user_id garantido no SQL (dims por first-event, unique test válido)
- [ ] Binomial exato DEFINE o gate em células ≤30; chi2 decide nas grandes
- [ ] Guards: braço vazio → ValueError; API Claude com try/except → fallback local
- [ ] Dataset contract único: sources em analytics_raw; models em analytics (profile)
- [ ] requirements.txt com versões fixadas; .gitignore; pytest vai no pacote de evidência
- [ ] Sem eventos futuros no CSV (max_ts ≤ 2026-10-23); calc flags removidos

## 3. Verificação independente recomendada
- pytest tests/ -q  (23 testes; esperado: 23 passed)
- Recompute forçado do payload (conferir AUDIT_REPORT.md tabela final)

## Veredito
- [AUDIT: PASSED] técnico-confirmado  |  [AUDIT: FAILED + correções]
