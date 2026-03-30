Your working directory is: {workspace_path}
It contains multiple repo directories (one per folder). Each repo is a git clone.

1. Look at the components listed above and find the matching repo folder(s).
   The folder names partially match the component names (e.g. component 'CMS UI' → folder 'cms').
   List the directories to find the right one(s).

2. For each repo you need to work in, create a git worktree:
   cd <repo-folder> && git fetch origin && git worktree add ../../worktrees/{key}-<short-slug> -b feature/{key}-<short-slug> origin/<default-branch>
   (create the worktrees/ directory under the workspace root if needed).

3. Do all implementation, tests, and commits inside the worktree directory.
   Use a meaningful commit message that references the ticket key.

4. Do NOT push and do NOT create a pull request.

5. After you are done, write a JSON manifest file at:
   {run_manifest}
   with this exact shape (valid JSON, no comments):
   {{
     "issue_key": "{key}",
     "worktrees": [
       {{
         "worktree_path": "<absolute path to the worktree>",
         "branch": "<the branch name you created>",
         "pr_title": "<concise PR title, max 72 chars, referencing {key}>",
         "pr_description": "<markdown PR description: what was changed and why, referencing {key}>"
       }}
     ]
   }}
   Include one entry per worktree you created.
   The pr_title and pr_description will be used verbatim when opening the pull request.
