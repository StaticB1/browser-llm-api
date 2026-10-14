# Contributing

Thanks for helping. This project drives live websites, so most changes are one of two kinds: logic that the unit tests can check, and selectors that only a real request can check. Both are welcome; the second kind needs a note on what you measured.

## Setting up

```bash
git clone https://github.com/StaticB1/browser-llm-api.git
cd browser-llm-api
scripts/ensure-venv.sh                  # ./venv with the dependencies and the package installed editable
DISPLAY=:1 ./venv/bin/browser-llm login chatgpt
./serve.sh
```

`BROWSER_LLM_PREWARM=0 BROWSER_LLM_PORT=8097 ./venv/bin/browser-llm serve` starts a second server that opens no browser, which is handy for working on the dashboard next to a running service. Do not move or cover a running service's Chrome windows; see [Running it](docs/running.md#the-display).

## Checks

```bash
./venv/bin/python -m ruff check .
./venv/bin/python -m unittest discover -s tests -v
```

Both must pass. The tests need no browser and no network. They cover the completion tracker, the access rules, attachments, the history and delete layer, the MCP protocol, the ChatGPT selector lists, usage limits, the design tools, the `.env` file and the command. Ruff checks pycodestyle, pyflakes and warnings only, at a line length of 110.

When you change how an answer is judged done, who may call what, the attachment layer, or anything that deletes, add or extend a test in the same change.

## When a site changes

Most breakage comes from a site changing its page. A redesign usually shows as `input box not found` with a note that the page still holds an editable element, or as answers that read empty while the page shows them.

1. Read the live page before editing. Each provider's Chrome has a debugging port (`pgrep -af 'user-data-dir=./chatgpt_profile'`); look at the composer, the send button, the answer turn and the upload input.
2. Measure every selector the provider uses on a real text answer, a code answer, an upload and, for ChatGPT, an image.
3. Add the new layout to each selector list instead of replacing the old one, so a page serving either layout works. The lists are at the top of each provider file.
4. Say in the pull request what you measured and on which date.

## Pull requests

- Keep a pull request to one change, with a title that says what it does.
- Describe what you verified: the checks above, and for selector changes, the live requests you ran.
- Update the docs page that covers what you changed, and add a line to the changelog under a heading for the next version.
- Do not commit browser profiles, `.env`, logs or generated images; `.gitignore` covers them.

## Releasing

Maintainers follow [docs/releasing.md](docs/releasing.md).

## Code of conduct

Be kind, assume good faith, and keep discussion about the work.
