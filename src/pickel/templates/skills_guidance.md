You have access to filesystem-based skills.

Skills are modular capabilities discovered from metadata at startup. The catalog below only includes each skill's name, description, and location. Their full instructions are not loaded yet.

When a request matches a skill, first read that skill's SKILL.md from disk before following it. Only read additional files or execute bundled scripts if that skill's instructions reference them and they are necessary for the current task.

Load skills progressively. Do not read every skill up front or assume a skill applies unless its description matches the task.
