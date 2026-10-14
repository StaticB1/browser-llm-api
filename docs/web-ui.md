# Web UI

Three interfaces sit on the same API: a web dashboard served by the server, a chat widget you can embed in any page, and a native Linux tray app.

## Dashboard

Open `http://localhost:8081/`. It is laid out like the chat apps people already use: a history sidebar, one centred reading column, plain assistant prose, grey user bubbles and a rounded composer. The theme switch in the sidebar footer has three states, light, auto and dark, and auto follows the system setting.

- Chat: streaming replies with multi-turn context. The send button becomes a stop button while an answer streams. Attach images with the plus button, by pasting, or by dropping them onto the composer. Code comes back as a card with its language and a Copy button.
- History: every conversation is kept in this browser, grouped by day and searchable. Rename or delete one from its row, delete all from the top-bar menu, or export one as JSON. A new chat is `Ctrl+Shift+O`.
- Import the account's history: "Import history" in the top-bar menu lists the conversations already in the signed-in ChatGPT account. A thread's messages load the first time you open it, because reading a whole account at once makes the site answer 429.
- Delete means delete: deleting a chat also deletes the images it made from disk and the conversation from the account, and the confirmation names all three counts first.
- Create image: a prompt makes an image, with a timer. Attach references to switch to image-to-image. The presets row picks a design kind, shape, theme, platform, headline space and art direction, and a design-system box holds colours and type to follow. Each result shows its size, the model that drew it, whether Create image mode was on, and the prompt that was sent.
- Gallery: every saved image, newest first, filterable by provider, with select and delete.
- Server: per-provider telemetry, server details, and a copy-paste embed snippet for the widget with a preview button.

The history and delete endpoints behind the dashboard answer only callers on the server's own origin, so open the dashboard on the server's own address.

## Embeddable widget

One line drops a floating chat bubble onto any page on your network:

```html
<script src="http://localhost:8081/widget.js"></script>
```

The widget is isolated in a shadow root, so the host page's CSS cannot leak in or out. It shares the dashboard's look, with code cards, a stop button, image attach, and full-screen sizing on a phone. It finds the server from its own script URL, and the thread survives a page reload. A demo runs at `http://localhost:8081/demo`.

| Attribute | Default | Meaning |
|---|---|---|
| `data-provider` | server default | `chatgpt-browser` or `gemini-browser` |
| `data-title` | `Ask AI` | header text |
| `data-greeting` | a short hello | the first assistant line |
| `data-system` | none | a system prompt sent with every turn |
| `data-position` | `br` | `br` bottom right, `bl` bottom left |
| `data-theme` | `auto` | `light`, `dark`, or `auto`, which follows the host page |
| `data-accent` | theme colour | one colour for the bubble and send button in both themes |
| `data-open` | closed | `1` starts it open |
| `data-persist` | `1` | `0` forgets the thread on reload |
| `data-attach` | `1` | `0` hides image attach |
| `data-key` | none | the API key, for a server that requires one |

`window.BrowserLLMWidget` has `open()`, `close()`, `reset()` and `config({...})`. `Esc` closes it.

The widget inherits the server's trust model: anyone who can load a page with it can use your signed-in session. Keep it on a trusted network.

## Desktop app

`desktop/` holds a native GTK3 tray app for Linux, a thin client of the same API that runs on the system Python with the standard library only. Its tray popup makes image assets for the VS Code project you have focused and saves them into it, and a full window has Chat, Images, Gallery and Status tabs. Setup and details are in [desktop/README.md](../desktop/README.md).
