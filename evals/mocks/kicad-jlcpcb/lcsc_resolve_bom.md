---
expect:
  rows: array
---
{
  "submitted": {{input.rows}},
  "unresolved": [],
  "note": "Every submitted row resolved. Rows given by C-number keep that part; rows given as a free-text query were matched against the catalog. Tier, stock and price for each C-number are as listed by lcsc_search.",
  "extended_parts": ["C2934560", "C173752"],
  "estimated_setup_fee_usd": "$3.00 per unique extended part in the resolved set"
}
