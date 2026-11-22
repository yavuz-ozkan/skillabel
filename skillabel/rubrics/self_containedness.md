# Rubric: self_containedness

Grades whether the skill is executable from what it ships: referenced files
exist, required tools and inputs are declared, and bundled scripts are
documented. A skill that points at missing paths or undeclared prerequisites
strands the agent mid-task. Ground every finding in a cited line.

**A** — Every relative path referenced in the body resolves to a bundled file;
every prerequisite (CLI tool, env var, input) is declared before use; every
bundled script is mentioned and explained. The skill runs from its own
directory with no undocumented assumptions.

**C** — Mostly self-contained with a gap or two: a prerequisite assumed but not
stated, or a bundled file present but not explained, or a reference that
resolves but is under-described. Recoverable with minor guesswork.

**F** — Dead references to files that do not exist, undocumented bundled
scripts, or hard dependencies (tools, keys, network services) never declared.
The agent cannot complete the skill from what is shipped.
