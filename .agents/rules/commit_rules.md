# Git Commit & Changelog Management Rule

Whenever the user asks you to commit changes, you MUST follow these steps:

1. **Analyze Changes**: Run `git diff` or evaluate the modified files to determine the scope of changes.
2. **Draft Message**: Generate a descriptive, clean commit title and bulleted list description of the changes.
3. **Update `changelog.md`**: Update the root [changelog.md](file:///d:/ThinkMate/changelog.md) with a new entry detailing:
   - Date and commit name.
   - List of files changed.
   - Short summary of the modifications.
4. **Commit**: Propose the commands to add all changes and perform the git commit.
