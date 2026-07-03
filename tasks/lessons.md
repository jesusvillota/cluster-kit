# Lessons

- Prefer YAML for workflow authoring when the user wants human-friendly staged
  pipelines, but keep raw `uv run ...` command blocks as the source of truth.
- When workflows are keyed by structure, infer the mode from the presence of
  `stages` or `jobs` so the file stays concise and ergonomic.
- For phone UI changes, confirm whether button removals are view-specific before
  removing controls globally across Queue, Nodes, and Logs.
