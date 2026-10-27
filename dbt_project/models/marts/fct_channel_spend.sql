-- fct_channel_spend: channel economics mart (BigQuery Standard SQL)
-- Monthly grain: ad spend per acquisition_channel joined to paid conversions
-- of the SAME month+channel. This is the auditable CAC denominator/numerator
-- contract — no lifetime mixing, no subqueries drifting from the sources.
-- Shared calendar spine: months come from the spend side, so months with
-- spend but zero conversions surface infinite CAC explicitly (safe divide).

WITH spend AS (

    SELECT
        channel                                          AS acquisition_channel,
        DATE_TRUNC(DATE(spend_date), MONTH)              AS spend_month,
        SUM(daily_spend)                                 AS spend_usd
    FROM {{ source('analytics_raw', 'daily_spend') }}
    GROUP BY acquisition_channel, spend_month

),

paid AS (

    SELECT
        acquisition_channel,
        DATE_TRUNC(DATE(paid_at), MONTH)                 AS spend_month,
        COUNT(*)                                         AS paid_users
    FROM {{ ref('saas_funnel') }}
    WHERE reached_paid
    GROUP BY acquisition_channel, spend_month

)

SELECT
    COALESCE(s.acquisition_channel, p.acquisition_channel) AS acquisition_channel,
    COALESCE(s.spend_month, p.spend_month)                 AS spend_month,
    COALESCE(s.spend_usd, 0)                               AS spend_usd,
    COALESCE(p.paid_users, 0)                              AS paid_users,
    -- explicit NULL when a month spent money but converted nobody:
    -- surfaced as data-quality input, never silently averaged away
    IF(COALESCE(p.paid_users, 0) > 0,
       COALESCE(s.spend_usd, 0) / p.paid_users,
       NULL)                                               AS cac_usd
FROM spend AS s
FULL OUTER JOIN paid AS p
    ON s.acquisition_channel = p.acquisition_channel
   AND s.spend_month = p.spend_month
