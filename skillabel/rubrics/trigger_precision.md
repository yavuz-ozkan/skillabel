# Rubric: trigger_precision

Grades how precisely the skill states *when it fires* and *when it must not*.
A skill's description is always in context; a vague trigger overfires (steals
turns from other skills) or underfires (never activates). Ground every finding
in a cited line.

**A** — The description and/or a "when to use" section names concrete trigger
phrases (quoted user intents, file extensions, named deliverables) AND states
explicit skip/negative conditions. A reader can predict activation with no
guessing. Trigger nouns/verbs are specific, not generic ("convert markdown to
Word", not "help with documents").

**C** — Triggers are present but partial: some concrete phrasing, but no skip
conditions, or the phrasing is broad enough to collide with neighboring skills.
A reader can mostly predict activation but edge cases are ambiguous.

**F** — The trigger is a generic capability statement with no quoted intents,
no examples, and no skip conditions ("use this for text tasks"). The skill will
overfire or fire unpredictably. No line in the skill pins down activation.
