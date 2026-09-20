---
expect:
  project_path: string
---
{
  "project_path": "{{input.project_path}}",
  "pro_file": "{{input.project_path}}/soil-node.kicad_pro",
  "sch_file": "{{input.project_path}}/soil-node.kicad_sch",
  "pcb_file": "{{input.project_path}}/soil-node.kicad_pcb",
  "pcb_exists": true,
  "libs_dir": "{{input.project_path}}/libs",
  "resume_available": true,
  "session": {
    "stage": "pcb_generated",
    "checkpoints": ["project_created", "parts_sourced", "bom_confirmed", "pcb_generated"],
    "bom_length": 13,
    "spec_present": true
  },
  "resume_summary": "A board was generated for this project on 2026-09-12: 13 components, 9 nets, 80x60mm, 2-layer. The .kicad_pcb has been modified since then (last write 2026-09-14).",
  "warnings": [
    "pcb_generate rebuilds the board from the spec and saves over this file. Any placement or routing done in KiCad since it was generated will be lost."
  ]
}
