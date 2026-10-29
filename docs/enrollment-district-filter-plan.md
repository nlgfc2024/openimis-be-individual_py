# Household enrollment district filter: openimis-be-individual_py

Status: implementation planning only. This branch currently contains documentation, not a working district filter.

## Branch and ownership

- Base: `mw/develop`; do not branch from the repository default if it differs.
- Feature branch: `feature/household-enrollment-district-filter`.
- Assignee: @YutakaX17.
- Requested reviewers: @mcleanka, @uniquedj95, @Lenwell89.
- Target project: https://github.com/orgs/nlgfc2024/projects/12 (project membership pending authorization).

## Behavior and shared contract

Keep the phase criterion `validation_status = VERIFIED` locked during enrollment. Under Add filters, offer `Location – District (R) | Equals | [Select district]` as an optional household operator filter. Select from active location master data within permitted scope, not a hard-coded list.

Use the schema property `district_id: {"type":"integer"}` on GROUP phases. A selected numeric database location ID serializes as `district_id__exact__integer=123`. An empty/cleared/removed selection contributes no condition. Do not put district in saved phase advanced_criteria, use an all-district sentinel, or duplicate district data in household json_ext.

The backend must use Group.location and district ancestry, intersect with authenticated user scope before eligibility counts/ranking, retain VERIFIED and existing phase-wide capacity rules, and apply identical rules to summary, preview and confirmation. A selected R can match its active descendants at any depth. No district selection must never disable access restrictions. Confirmation revalidates current eligibility.

## Small reviewable commits

Issue order below is the intended commit sequence in this repository. Each implementation commit includes its focused regression tests and references its issue. Record actual test commands/results and commit SHA in the issue; do not mark work complete based on this planning file.

### BE-1: Define and validate the household district enrollment filter

Issue: https://github.com/nlgfc2024/openimis-be-individual_py/issues/19

Suggested commit: `feat(enrollment): define and validate district filter`

Update individual/custom_filters.py and individual/services.py. Expose district_id only when enabled in the GROUP phase schema; advertise integer/exact only. Validate a numeric database ID referencing an active type-R location. Keep generic json_ext filters and individual enrollment behavior unchanged.

Acceptance:

- [ ] Discovery returns district_id with integer type and exact operator only for enabled GROUP schemas.
- [ ] Malformed/empty IDs, unsupported operators/types, inactive or non-R locations, and fields absent from the phase schema are rejected clearly.
- [ ] Focused validation/discovery tests cover positive and negative cases.

Dependencies: none.

### BE-2: Filter households by district ancestry and authenticated user scope

Issue: https://github.com/nlgfc2024/openimis-be-individual_py/issues/20

Suggested commit: `feat(enrollment): intersect district ancestry with user location scope`

Handle the reserved district_id field through Group.location rather than json_ext. Reuse validated location hierarchy helpers, include direct R assignments and active descendants at any depth, and enforce the authenticated user's permitted locations in the common selection path before ranking/counts. Thread user context where needed. Omission must retain access restrictions; a partially authorized district must not grant all its households.

Acceptance:

- [ ] Selected district matches only authorized households assigned to it or its descendants; sibling districts never match.
- [ ] Without district, all existing eligibility and user-scope rules still apply.
- [ ] Unauthorized direct API requests fail under established access policy; partial-district access remains an intersection.
- [ ] Hierarchy, restricted user, missing-location, and non-district regression tests pass.

Dependencies: https://github.com/nlgfc2024/openimis-be-individual_py/issues/19.

### BE-3: Verify district scope across enrollment summary, preview, and confirmation

Issue: https://github.com/nlgfc2024/openimis-be-individual_py/issues/21

Suggested commit: `test(enrollment): verify district preview and enrollment parity`

Trace individual/schema.py, gql_mutations.py, services.py and any active asynchronous workflow. Ensure summary, list and confirmation use the shared scoped eligibility rules. Add API-level regression tests near test_authoritative_enrollment.py, with narrowly scoped wiring corrections only if tests expose gaps. Preserve mandatory VERIFIED rules, existing assignments, ranking and phase-wide capacity.

Acceptance:

- [ ] Summary, preview list, and mutation use equivalent eligibility with and without district.
- [ ] Omitting VERIFIED in browser data cannot bypass saved phase criteria.
- [ ] No out-of-scope household data leaks through returned counts; capacity semantics remain phase-wide.
- [ ] Confirmation recomputes current eligibility and background execution retains authorized scope where applicable.

Dependencies: https://github.com/nlgfc2024/openimis-be-individual_py/issues/20.

## Cross-repository integration and review

- FE-1: https://github.com/nlgfc2024/openimis-fe-individual_js/issues/23
- FE-2: https://github.com/nlgfc2024/openimis-fe-individual_js/issues/24
- FE-3: https://github.com/nlgfc2024/openimis-fe-individual_js/issues/25
- CORE-1: https://github.com/nlgfc2024/openimis-fe-core_js/issues/25
- APP-BE-1: https://github.com/nlgfc2024/openimis-be_py/issues/15
- APP-BE-2: https://github.com/nlgfc2024/openimis-be_py/issues/16
- APP-FE-1: https://github.com/nlgfc2024/openimis-fe_js/issues/9
- APP-FE-2: https://github.com/nlgfc2024/openimis-fe_js/issues/10

Backend and frontend module work must merge before assembly references are updated. Staging uses test configuration; production defaults and existing-phase schema updates follow successful integrated acceptance. Preserve unrelated schema properties, document export/rollback, and avoid applying production configuration in this planning PR.

Check current enrollment refactors before implementation: backend PRs https://github.com/nlgfc2024/openimis-be-individual_py/pull/8, https://github.com/nlgfc2024/openimis-be-individual_py/pull/17 and https://github.com/nlgfc2024/openimis-be-individual_py/pull/18, and frontend PR https://github.com/nlgfc2024/openimis-fe-individual_js/pull/22. Rebase on current mw/develop and adapt paths if enrollment ownership moves; do not merge unrelated work into this feature merely to avoid resolving overlap.

Keep commits separated by issue for reviewer navigation. Add new commits for review fixes where practical; no implementation issue is closed by the initial documentation commit. Before marking the feature ready, update the PR description to describe actual delivered behavior, actual validation, remaining risks, and completed issue links. Since mw/develop may not be the repository default branch, verify issue closure explicitly after implementation is merged rather than relying solely on automatic closing keywords.

