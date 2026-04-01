You are helping clarify a vague ticket description by reading the actual codebase.

Read the ticket description below and identify anything that is ambiguous, underspecified, or assumes behaviour that may differ from what the code actually does. Then read the relevant parts of the codebase to resolve those ambiguities — for example, if the description says "change how X works", find how X works today.

Write your findings to {context_path} as a plain-text summary. Cover only what is needed to make the description unambiguous:
- What the current behaviour is (where the description is vague or assumes it)
- Which files, functions, or components are involved
- Any constraints or edge cases visible in the existing code that the description overlooks

Do not suggest improvements or rewrite the ticket. Report only what you observe in the code.

Ticket description:
{raw_description}
