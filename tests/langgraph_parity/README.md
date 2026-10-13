# `tests/langgraph_parity/` — LangGraph parity suite

The consolidated LangGraph parity home: each test in this directory pins one
P1 tier of the LangGraph feature contract the executor must match, namely
P1.1 conditional edges (typed predicates over node outputs),
P1.2 checkpointer (state persistence + resume), P1.3 `run --resume` (the
executor writes the ledger; a resume re-dispatches only what did not
succeed), P1.4 per-node retry
(declarative retry blocks), and P1.5 parity consolidation itself (the
sharded collection under this directory is the single parity surface).