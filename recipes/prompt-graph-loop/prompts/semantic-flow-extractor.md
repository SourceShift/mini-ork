Read the declared artifact inputs. Produce exactly one JSON object with:

- `tasks`: ordered work units with stable IDs.
- `actors`: humans, agents, tools, and external systems.
- `artifacts`: named input, intermediate, and output artifacts.
- `risks`: risk, impact, mitigation, and owner records.
- `missing_context`: facts that would materially change the graph.
- `acceptance_criteria`: observable completion checks.

When `human_feedback` is present, treat it as authoritative scope feedback and
replace superseded assumptions. Do not return prose outside the JSON object.
