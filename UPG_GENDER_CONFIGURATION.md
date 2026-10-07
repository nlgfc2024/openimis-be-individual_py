# Configurable UPG head gender and quotas

Merge this into the UPG phase's existing **json_ext** in Django admin:

```json
{
  "upg_head_gender_options": ["FEMALE", "MALE", "BOTH"]
}
```

Preserve `advanced_criteria` and other saved configuration. This is phase
configuration, not `openimis.json` or the older descriptive `programmes` file.
Only the listed options are displayed and accepted by the backend. For example,
`["FEMALE", "BOTH"]` hides/rejects Male-only selection. If the key is absent,
the existing Female/Male behavior is preserved.

When Both is selected, enter whole-number Female and Male percentages totaling
100. Set the phase's **max_beneficiaries** first; Both is rejected without a
maximum. Percentages split the remaining places (maximum minus current
enrollment for the selected phase/status). Existing ranking percentage limits,
if any, can further reduce that target. This is a split of this intake, not a
rebalance of households already enrolled. Shortfalls remain unfilled.

Example: maximum 100, already enrolled 20, Female 60%, Male 40% gives 48 female
places and 32 male places. If only 30 female households qualify, selection is
30 female and up to 32 male. Fractional places use nearest-integer rounding for
female places (half ties go to female), with male receiving the remainder.

Existing system, phase and operator criteria remain mandatory before quotas.
Households are ranked within each gender using the existing phase ranking,
or ID order if no ranking is configured. Current non-deleted heads and people
are used. Households with conflicting male and female head records cause a
validation error rather than being double-counted. Preview and enrollment use
the same selection function.

No enrollment or phase configuration is modified by installing this code.

Checks:

```powershell
& .\.venv311\Scripts\python.exe openimis-be-individual_py\checks\gender_quota_check.py
```
