# target/ is the repo the SDD e2e dry-run copies and implements; its
# test_multiply.py fails by design until multiply() exists, so never collect it.
collect_ignore = ["target"]
