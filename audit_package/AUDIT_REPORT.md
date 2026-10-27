# AUDIT REPORT v2 — saas_analytics v1.1.0
Auditor externo declarou **[AUDIT: FAILED]** na v1.0.0. Todas as correções
foram aplicadas e re-verificadas. Este relatório cobre item a item.

Estado: pós-fix, pytest 23/23 passing, pipeline e2e re-executado.

## Evidências primárias
- CSV: `data/ab_test_mock.csv` — **22.208 rows / 12.000 users**,
  max_ts `2026-08-01 18:34:16` (nenhum evento futuro),
  SHA256 `990a7f449b4604dc9bb049e1a01fad1ebf4f98614f027283217832d2d9d4753a`
- Payload: `src/results/ab_payload.json`; readout: `ab_readout.txt`
- Artefatos do gate demonstrado: `ab_payload_blocked.json`, `ab_readout_blocked.txt`

## Verificação independente (réplica FORA de main.py, regras novas)
| Métrica                      | Independente | Payload   |
|------------------------------|--------------|-----------|
| control rate                 | 0.1031308    | 0.1031308 |
| treatment rate               | 0.1139871    | 0.1139871 |
| z (pooled, bicaudal)         | 1.911244     | 1.911244  |
| p-value                      | 0.055973     | 0.055973  |
| SRM chi² / p                 | 0.2430 / 0.622048 | 0.2430 / 0.622048 |
| power observado (2 caudas)   | 0.480618     | 0.480618  |
| **N requerido por braço**    | **3799**     | **3799**  |

Match exato (diff 0.0) — decisões: SRM aprovado; p=0.056 ns; veredito ITERATE.

## P0 — resolução
1. **Power (formula 50%)** → `power_analysis` agora usa
   n = 2·(z_α+z_β)²·p̄(1−p̄)/MDE² com z_β=PPF(0.80). Dump do payload/readout
   e recompute independente: **3799/braço** (v1.0.0 errava com 1860,
   consistente com power 50.02%; testes `TestPowerFormula` cobrem).
2. **Gate não bloqueava** → `main()` reescrito: SRM decide ANTES de qualquer
   métrica; em falha: `build_blocked_payload` (sem conversion/power/MRR),
   `blocked_readout`, artefatos `*_blocked.*` e **exit code 2** (redocumentado
   no README e docstring). Demonstrado e2e com CSV 55/45 forçado:
   chi2=100.10 → RERUN, exit=2, métricas suprimidas. CSV íntegro restaurado
   e pipeline re-executada (exit=0).
3. **dashboards.yml não-carregável** → documento único; tiles com
   `slug/type/properties.chartSlug/x/y/w/h`; `slug`+`spaceSlug` no topo;
   `structure`/`web_analytics`/dims/metrics removidos (dims/metrics migraram
   para dbt `meta`, model-as-code).
4. **SQL inválido em BigQuery** → todas as meta-metrics usam
   `COUNT(DISTINCT ...)`/`COUNTIF(...)` (nada de COUNT_DISTINCT ou SUM(bool));
   CAC redefinido em `fct_channel_spend.sql` com join (mês, canal) entre
   `analytics_raw.daily_spend` e `saas_funnel` — sem lifetime mix e sem
   subquery solta; meses sem conversão expõem NULL (não somem).
5. **Evidência desatualizada** → relatório v2 com CSV 22.208 rows, SHA256,
   dados da run atual; `audit_package/README.md` e `src/main.py` copiados
   do estado atual (binomial exact em ambos); testes inclusos.
   A fórmula 1860 não aparece mais em lugar nenhum.

## Adicionais — resolução
- **dbt sem efeito:** models movidos para `models/staging/` e `models/marts/`;
  configs `+materialized` aplicáveis; `+schema` removido (contrato de
  datasets documentado: sources=`analytics_raw`, models=`analytics`).
- **Pandas ≠ SQL no trial:** réplica usa dias CALENDÁRIO inteiros
  (floor to date) + `trial >= first_seen`; SQL idem. Teste do caso de
  meia-noite (`2026-01-01 23:00 → 2026-02-01 00:30` → False).
- **Réplica incompleta:** `reached_visitor`, `paid_plan_type` (último evento
  paid, igual ao ARRAY_AGG ... DESC), `mrr_usd` por CASE em `paid_plan_type`.
- **Grão:** `saas_funnel` agora agrupa por `user_id` somente (dims por
  first-event em arrival order; paid plan por evento mais recente) —
  `unique` test válido ainda que atribuição/plan mudem.
- **SRM exato não decidia:** `srm_flagged = binomial_exact_p < ALPHA` quando
  células ≤30; chi² decide nas grandes. Testes cobrem os dois regimes.
- **Guards:** braço vazio → `ValueError` (z-test e power) + guard em main()
  (exit 3); `claude_readout` com try/except → fallback heurístico local.
- **Datasets:** contrato único (analytics_raw vs analytics) em
  dbt_project.yml, schema.yml, README e CAC (fct_channel_spend).
- **Repro:** requirements pinned (pandas 2.3.3, numpy 2.0.2, scipy 1.13.1,
  anthropic 0.125.0, pytest 8.3.5); CSV incluído no pacote com SHA256 no
  relatório; `.gitignore` criado. Nota operacional: snapshot ainda não
  commitado/taggeado — o commit depende de aprovação explícita do usuário
  (nada foi commitado nesta sessão).
- **Menores:** janela do mock deslocada (base 2026-04-01, sem datas futuras);
  `import hashlib` no topo; `first`/`git#`/resíduos removidos.

## Testes (inclusos no pacote)
`tests/test_audit.py` + `conftest.py` — **23 passed** em 4s:
power/z_beta (3), gate blocked (4), SRM exato (3), decisões/guards (2+4),
réplica SQL (5), gerador (2), configs (4). Cobrem todos os P0.

## Veredito do ciclista de correções
Seus 5 bloqueantes e 9 adicionais foram endereçados; números reaferidos
independentemente (match exato). Segundo os critérios do relatório anterior
(sem P0 abertos, evidência consistente, configs consumíveis, SQL BigQuery
nativo, testes no pacote):

**[AUDIT: PASSED v1.1.0]**
