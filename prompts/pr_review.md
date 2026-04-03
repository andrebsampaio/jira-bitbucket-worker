You have been asked to review PR "{pr_title}" in repo {repo_slug} (branch: {source_branch}).

Diff:
{diff}

Perform a thorough code review. Focus on correctness, security, performance, readability, and missing tests or edge cases. Only flag concrete issues you can justify from the diff. If everything looks good, explicitly say so.

Write your findings to a JSON file named .codex-review.json in the current working directory with the following shape (valid JSON, no comments):
{
  "status": "approve" | "changes_requested",
  "summary": "<overall review summary>",
  "comments": [
    {
      "path": "<relative file path>",
      "line": <line number on the new code>,
      "severity": "nit" | "minor" | "major",
      "message": "<actionable feedback for that line>"
    }
  ]
}

Guidelines:
- Include at most 10 comments, prioritizing the most impactful findings.
- If no issues are found, set status to "approve", provide a positive summary, and use an empty comments array.
- Only reference files/lines that exist in the diff.
- Do NOT modify any repo files besides writing .codex-review.json, and do not run git commands.
