-- saas_funnel: user-level funnel mart (BigQuery Standard SQL)
-- Grain: ONE row per user (dims resolved by first event in arrival order),
-- so the schema-level `unique` test on user_id holds even if a user's
-- attribution or plan changes mid-funnel.
-- Stage flags encode the SaaS conversion funnel:
--   Visitor -> Trial -> Activated -> Paid
-- Consumed by Lightdash for all growth metrics (signups, rates, MRR).

WITH user_funnel AS (

    SELECT
        user_id,
        -- first-seen dimensions: arrival order, not last-state
        ARRAY_AGG(ab_group ORDER BY entered_at LIMIT 1)[SAFE_OFFSET(0)]            AS ab_group,
        ARRAY_AGG(acquisition_channel ORDER BY entered_at LIMIT 1)[SAFE_OFFSET(0)] AS acquisition_channel,
        ARRAY_AGG(plan_type ORDER BY entered_at LIMIT 1)[SAFE_OFFSET(0)]           AS plan_type,
        MIN(IF(stage = 'visitor', entered_at, NULL))    AS first_seen_at,
        MIN(IF(stage = 'trial', entered_at, NULL))      AS trial_started_at,
        MIN(IF(stage = 'activated', entered_at, NULL))  AS activated_at,
        MIN(IF(stage = 'paid', entered_at, NULL))       AS paid_at,
        -- paid plan resolved from the user's most recent 'paid' event
        ARRAY_AGG(IF(stage = 'paid', plan_type, NULL) IGNORE NULLS
                  ORDER BY entered_at DESC LIMIT 1)[SAFE_OFFSET(0)]            AS paid_plan_type
    FROM {{ ref('stg_visitors') }}
    GROUP BY user_id

),

flags AS (

    SELECT
        user_id,
        ab_group,
        acquisition_channel,
        plan_type,
        first_seen_at,
        trial_started_at,
        activated_at,
        paid_at,
        paid_plan_type,
        first_seen_at IS NOT NULL AS reached_visitor,

        -- trial requires an actual forward step AND <= 30 whole calendar
        -- days (DATE_DIFF on DATEs; same rule mirrored in build_funnel)
        trial_started_at IS NOT NULL
            AND trial_started_at >= first_seen_at
            AND DATE_DIFF(DATE(trial_started_at),
                          DATE(first_seen_at), DAY) <= 30          AS reached_trial,

        activated_at IS NOT NULL
            AND trial_started_at IS NOT NULL
            AND activated_at >= trial_started_at                   AS reached_activated,

        paid_at IS NOT NULL
            AND trial_started_at IS NOT NULL
            AND activated_at IS NOT NULL
            AND paid_at >= activated_at                            AS reached_paid

    FROM user_funnel

)

SELECT
    user_id,
    ab_group,
    acquisition_channel,
    plan_type,
    first_seen_at,
    trial_started_at,
    activated_at,
    paid_at,
    paid_plan_type,
    reached_visitor,
    reached_trial,
    reached_activated,
    reached_paid,
    CASE
        WHEN reached_paid AND paid_plan_type = 'starter'    THEN 29.0
        WHEN reached_paid AND paid_plan_type = 'pro'        THEN 99.0
        WHEN reached_paid AND paid_plan_type = 'enterprise' THEN 399.0
    END AS mrr_usd
FROM flags
