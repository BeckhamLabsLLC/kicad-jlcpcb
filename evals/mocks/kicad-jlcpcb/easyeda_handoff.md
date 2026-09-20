---
expect: {}
---
{
  "pcb_path": "/home/user/pcb/soil-node/soil-node.kicad_pcb",
  "next_steps": [
    "Open https://easyeda.com/editor (sign in — free account OK)",
    "File -> Import -> KiCad, choose soil-node.kicad_pcb",
    "Route -> Auto Route, then review the result",
    "PCB Order via JLCPCB when you are happy with the routing"
  ],
  "why_easyeda": "EasyEDA's cloud auto-router handles real boards, including RF matching networks, that open-source routers cannot solve. It is owned by the same company as JLCPCB, so ordering is one click.",
  "before_you_order": [
    "Check the assembly preview: JLCPCB's expected rotation differs from KiCad's for some packages.",
    "Confirm every extended-tier part is one you meant to pay the setup fee for.",
    "Confirm the layer count on the order form matches the board."
  ],
  "alternative": "If you would rather stay in KiCad, route there and use package_for_jlcpcb to produce the upload zip."
}
