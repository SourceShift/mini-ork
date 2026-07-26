Read the declared agent graph, verification report, and human decision. Export
valid Python source only, with no Markdown fences. Use `import dspy` and define
one `dspy.Signature` per approved graph role plus a `dspy.Module` that follows
the approved dependency order. Do not emit an in-process scheduler that ignores
the graph; preserve the graph as a data declaration and leave execution to the
host runtime.

Refuse to export if the human decision is not `approved`; explain the reason in
a Python comment at the top of the file.
