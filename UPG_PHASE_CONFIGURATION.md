# UPG phase criteria

Paste `upg-phase-config.json` into the UPG Benefit Plan's `json_ext` in Django
admin. Merge with existing configuration: preserve any existing advanced
criteria and ranking. This is not host openimis.json or Module Configuration.
The phase name or code must identify UPG, and the phase type must be GROUP.

Supported `upg_criteria` keys:

| Key | Meaning |
| --- | --- |
| previous_programme | Previous enrollment plan code/name (case-insensitive; name also supports contains, preserving existing behavior) |
| enrollment_status | Required status on that enrollment; null skips this check |
| participant_status | Required enrollment json_ext.participant_status, case-insensitive; null skips this check |
| member_min_age / member_max_age | Inclusive age bounds, integers from 0 through 120 |
| requires_livelihood_activity | Require the same age-qualified member to have individual.json_ext.livelihood_activity true, "Yes", or "YES" |
| validation_statuses | Accepted household Group.json_ext.validation_status values; empty list skips this check |

All configured conditions AND together, alongside existing advanced criteria.
The previous enrollment must not be deleted. The qualifying member and their
household link must not be deleted. The head may be a different member.
Gender options remain under `upg_head_gender_options`; percentage inputs remain
in the UI when Both is selected. Set max_beneficiaries separately on the phase.

The example requires ACTIVE SCTP enrollment AND participant_status YES. Set
participant_status to null if ACTIVE enrollment alone is sufficient.

Without upg_criteria, legacy defaults remain SCTP participation YES, age 18–64,
no enrollment status restriction, no livelihood requirement, no validation
status restriction. Unknown keys and invalid values are rejected. This change
does not make RMEP rules configurable and does not alter saved phase data.

After installing/rebuilding the modules, restart backend/frontend processes
as needed and reload/reselect the phase. No database migration is required.
