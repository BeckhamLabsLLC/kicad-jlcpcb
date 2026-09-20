---
expect:
  project_path: string
---
{
  "project_path": "{{input.project_path}}",
  "session_found": true,
  "stage": "pcb_generated",
  "checkpoints": ["project_created", "parts_sourced", "bom_confirmed", "pcb_generated"],
  "bom_length": 13,
  "spec_present": true,
  "next": "handoff_rendered"
}
