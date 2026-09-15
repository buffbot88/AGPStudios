YOU MUST ADHERE TO THE FOLLOWING PRACTICES FOR DEVELOPMENT

1. Never rationalize for more than a quick moment without asking user.
2. Never attempt shortcuts.
3. Never hallucinate, assume, or decide without asking user for consent.
4. Never create scaffolds/mock/boilerplates/AI Slop/or underbuild.
5. Think rarely and on a budget: mechanical work (searches, lints, single-file edits, test runs) acts immediately with zero deliberation; anything warranting planning gets exactly ONE deliberate pass, and that pass must be visible as a written plan — never silent reasoning loops, repeated re-reads, or same-model re-derivations.
6. Gather ALL context first, then plan with the best reasoning path available: start file-picker + code-searcher in parallel, read every file the change touches (symbols, current behavior, conventions, tests), and produce a solid build plan in the standard format (goal → files to touch with why → change list → risks → validation) — via a Thinker agent when one is available, otherwise by planning directly with an adversarial review before the plan reaches the user.
7. Must ask the user if they approve of the build plan before implementing.
8. Docstrings can be NO LONGER than 1 or 2 sentences.
9. Do NOT write outside project directory without explicit permission.
10. Audit for stale docs, files, and codes to be removed.

BUILD PROTOCOL — Mechanical work acts immediately. Anything warranting a plan gets exactly one deliberate, context-complete planning pass (Vows 5–6); that plan is shown for approval before the first edit (Vow 7); implementation proceeds only on approval, then is validated and reviewed.