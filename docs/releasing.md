# Releasing

A release is a version bump, a changelog entry, a tag, and a GitHub release with the built files attached.

1. Pick the version: a major bump for anything that breaks how people install or call the project, a minor bump for additions, and a patch for fixes. Set it in `browser_llm/_version.py`.
2. Write the changelog entry in `CHANGELOG.md`, with the date and an "Upgrading" note for a major version.
3. Run the checks from the checkout:
   ```bash
   ./venv/bin/python -m ruff check .
   ./venv/bin/python -m unittest discover -s tests
   ```
4. Build and test the package in a clean environment:
   ```bash
   python3 -m venv /tmp/blm-build && /tmp/blm-build/bin/pip install build
   /tmp/blm-build/bin/python -m build --outdir dist .
   python3 -m venv /tmp/blm-try && /tmp/blm-try/bin/pip install dist/*.whl
   /tmp/blm-try/bin/browser-llm version
   ```
5. Check it live on a machine with signed-in sessions: one text answer, one image, and one MCP handshake through `mcp.sh`. Check `/api/status` shows the new version.
6. Commit, tag and push:
   ```bash
   git commit -am "Release X.Y.Z"
   git tag -a vX.Y.Z -m "X.Y.Z"
   git push origin master --tags
   ```
7. Publish the GitHub release with the changelog entry as its notes and the two files from `dist/`:
   ```bash
   gh release create vX.Y.Z dist/* --title "X.Y.Z" --notes-file <the entry>
   ```
8. Restart running installs once nothing is in flight (`browser-llm health` shows every provider idle).

Repository settings on GitHub (description, topics, private vulnerability reporting, branch clean-up) are kept in `scripts/github-settings.sh`. An admin runs it after a change to any of them; `--dry-run` shows what it would do.

The CI workflow runs the same checks on every push. When it cannot run, run steps 3 and 4 by hand and say so in the release notes.
