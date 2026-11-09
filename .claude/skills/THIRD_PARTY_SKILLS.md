# Third-party bundled skills

These skills are copied from MIT-licensed projects; each folder keeps the upstream `LICENSE`.
They are adapted so they work for AutoForge agents, which run inside a project, without a human
to ask and without subagents.

| Skill | Source | Commit |
|-------|--------|--------|
| `ui-ux-pro-max` | https://github.com/nextlevelbuilder/ui-ux-pro-max-skill (`.claude/skills/ui-ux-pro-max/`) | `4f2dbb67e64969a9a30ab00742f2d55d66ddec2b` (2026-11-08) |
| `test-driven-development`, `systematic-debugging`, `verification-before-completion` | https://github.com/obra/superpowers (`skills/`) | `22f0b463e7a5e579904dd6211a520321035526f7` (2026-11-03) |

## Adaptations

- `ui-ux-pro-max`: the upstream skill tests (`scripts/tests/`) are left out. `SKILL.md` runs the
  search script as `python3 .claude/skills/ui-ux-pro-max/scripts/search.py` from the project root
  (upstream uses `${CLAUDE_PLUGIN_ROOT}`, which is unset in a project), with a Grep/Read fallback
  on `data/*.csv`.
- superpowers skills: `superpowers:<name>` references became plain skill names; "ask / discuss
  with your human partner" became the AutoForge equivalent (`feature_report_failure`,
  `feature_mark_failing`, `feature_split`); `test-driven-development` says it does not apply in
  YOLO mode.

`test_bundled_skills.py` guards these adaptations. When updating from upstream, re-apply them.
