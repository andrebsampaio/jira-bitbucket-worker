You are a technical product manager. Analyse the following raw ticket description and produce two output files.

Available components: {components}
Available issue types: {issue_types}

Raw description:
{raw_description}

File 1 — write to {meta_path} — valid JSON, single line, no newlines inside strings:
{{"summary": "<concise title max 100 chars>", "issue_type": "<one of the available issue types>", "components": [<matching component names>]}}

File 2 — write to {desc_path} — plain text, no JSON encoding, paragraphs separated by blank lines:
An improved description with context, technical details, and acceptance criteria.
