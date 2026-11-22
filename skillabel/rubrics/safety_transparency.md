# Rubric: safety_transparency

Grades whether the skill is *honest about its own surface area* — not whether
it is "safe" (Skillabel never renders that verdict). A trustworthy skill states
plainly that it runs shell, touches the network, installs packages, reads
secrets, or writes outside its directory. The failure mode this dimension
punishes is prose that *claims* harmlessness while the body does otherwise.
Ground every finding in a cited line.

**A** — The skill's stated scope matches its actual operations. If it runs shell
or fetches from the network, it says so up front; side effects are disclosed
where they occur. No safety claim contradicts the body.

**C** — Operations are present and visible in the steps but not summarized or
flagged up front; a reader learns the surface area only by reading every line.
Nothing is actively misrepresented, but disclosure is incidental.

**F** — The description or intro *asserts* safety, harmlessness, or "no side
effects" while the body runs shell commands, fetches URLs, or otherwise acts on
the system. Stated scope contradicts real behavior — the exact wolf-in-sheep's-
clothing pattern. Cite the contradicting lines.
