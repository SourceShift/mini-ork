# Delivery Planner — post_mvp_delivery

Turn the selected option from `selected-option.md` and the researched options
from `options.md` into a delivery plan.

If the user has not selected an option, produce `${MINI_ORK_RUN_DIR}/delivery-plan.md`
as a decision checkpoint only. Do not claim implementation is ready.

If an option is selected, produce:

- implementation phases
- files or modules likely touched
- tests and validation gates
- rollback plan
- open questions
- confidence and risk rating

Write the delivery plan to `${MINI_ORK_RUN_DIR}/delivery-plan.md`.
