---
expect:
  spec: object
---
{
  "pcb_path": "{{input.spec.name}}.kicad_pcb (in the active project directory)",
  "board": "{{input.spec.name}}",
  "errors": [],
  "warnings": [
    "pcb_generate rebuilds the board from the spec; any manual placement or routing in the previous .kicad_pcb has been replaced."
  ],
  "next": "Call easyeda_handoff for the import and ordering instructions."
}
