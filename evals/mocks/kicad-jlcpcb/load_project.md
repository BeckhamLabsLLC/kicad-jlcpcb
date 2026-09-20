---
expect:
  project_path: string
---
{
  "project_path": "{{input.project_path}}",
  "pro_file": "{{input.project_path}}/board.kicad_pro",
  "sch_file": "{{input.project_path}}/board.kicad_sch",
  "pcb_file": "{{input.project_path}}/board.kicad_pcb",
  "libs_dir": "{{input.project_path}}/libs",
  "active": true
}
