-- stg_visitors: typed staging stream over the raw funnel event log
-- (BigQuery Standard SQL). Grain: one row per (user, stage) reached.
-- Built to be consumed by saas_funnel.

WITH raw_funnel_events AS (

    SELECT
        user_id,
        ab_group,
        acquisition_channel,
        plan_type,
        stage,
        CAST(entered_at AS TIMESTAMP) AS entered_at
    FROM {{ source('analytics_raw', 'funnel_events') }}

)

SELECT
    user_id,
    ab_group,
    acquisition_channel,
    plan_type,
    stage,
    entered_at,
    DATE(entered_at) AS stage_date
FROM raw_funnel_events
WHERE entered_at IS NOT NULL
