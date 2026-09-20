---
expect:
  parent_dir: string
  name: string
---
{
  "project_path": "{{input.parent_dir}}/{{input.name}}",
  "pro_file": "{{input.parent_dir}}/{{input.name}}/{{input.name}}.kicad_pro",
  "sch_file": "{{input.parent_dir}}/{{input.name}}/{{input.name}}.kicad_sch",
  "libs_dir": "{{input.parent_dir}}/{{input.name}}/libs",
  "manufacturing_dir": "{{input.parent_dir}}/{{input.name}}/manufacturing",
  "design_rules": "JLCPCB 2-layer defaults applied",
  "session": {"stage": "project_created", "checkpoints": ["project_created"]}
}
