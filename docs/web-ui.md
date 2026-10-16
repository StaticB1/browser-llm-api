# Web UI

Three interfaces sit on the same API: a web dashboard served by the server, a chat widget you can embed in any page, and a native Linux tray app.

## Dashboard

Open `http://localhost:8081/`. A slim rail on the left switches between four views, Chat, Studio, Gallery and Server, and on a phone the rail becomes a tab bar at the bottom. Each view has its own address (`#chat`, `#studio`, `#gallery`, `#server`), so the back button and bookmarks work. The look follows one system in both themes: a near-black canvas in dark, hairline borders, one lavender accent and the Geist typeface, which the server ships in `/assets` so the page looks the same with no network. The appearance button in the rail switches between light, dark and the system setting.

`Ctrl+K` (`Cmd+K` on a Mac) opens a palette that searches every chat by title and message text and runs any command: new chat, switch model, change the theme, import history, export a chat, copy the widget snippet. `?` lists the keyboard shortcuts; `Alt+1` to `Alt+4` switch views and `Ctrl+B` shows or hides the chat list.

- Chat: the chat list sits beside the conversation on a wide screen and slides in as a drawer on a phone. Answers stream in, or arrive whole from ChatGPT, under a line naming the provider, the model that actually answered and how long it took. While you wait, the turn says what is happening and counts the seconds, and the send button becomes a stop button. Hover a message you sent to edit it: everything after it is dropped and the edited text goes again, with its images. A failed turn shows the server's reason and a Try again button. Attach images with the paperclip, by pasting or by dropping them on the composer. Code comes back as a card with its language, a wrap toggle and Copy. Click any image to open it in the viewer.
- History: every conversation is kept in this browser, grouped by day and searchable. Rename, export or delete one from its row, select several to delete, or delete all from the menu. A new chat is `Ctrl+Shift+O`.
- Import the account's history: the import button above the chat list reads the conversations already in the signed-in ChatGPT account. A thread's messages load the first time you open it, because reading a whole account at once makes the site answer 429.
- Delete means delete: deleting a chat also deletes the images it made from disk and the conversation from the account, and the confirmation names all three counts first.
- Studio: write a brief on the left, pick a design kind from the cards and a shape, and Generate (`Ctrl+Enter`). Theme, platform, headline space, art direction and a design-system box sit under the kinds. Attach references, or reuse a result as one, to switch to image-to-image. While it works, a frame in the chosen shape counts the seconds. Each result shows its size, the model that drew it, whether Create image mode was on, the brief and the prompt that was sent, with buttons to download it, use it as a reference, send it to a chat or reuse the brief. Recent results stay listed in this browser. When the server has read ChatGPT's image counter, the number left and the reset time appear under Generate.
- Gallery: every saved image, newest first and grouped by day, loading more as you scroll. Filter by provider, open any image in the viewer with its brief, size and model (for images made since 2.1), step through with the arrow keys, and download, reuse, copy the link or delete from there. Select mode deletes several at once.
- Server: a headline says whether the server is ready, busy or failing, with uptime, default model, display and version. Each provider has a card with a chart of its last 40 request times (failures in red, the dashed line is the average), its counters, the image counter and its last error. Below them, a widget builder writes the embed snippet as you change the title, model, greeting, suggested prompts, system prompt, theme, position and accent, with a live preview.

The history and delete endpoints behind the dashboard answer only callers on the server's own origin, so open the dashboard on the server's own address.

## Embeddable widget

One line drops a floating chat bubble onto any page on your network:

```html
<script src="http://localhost:8081/widget.js"></script>
```

The widget is isolated in a shadow root, so the host page's CSS cannot leak in or out. It shares the dashboard's design: the lavender accent, hairline borders and Geist type, which it loads from the server's `/assets` so it looks the same on a page with no fonts of its own. Answers render with code cards and Copy, images open in a viewer with a download link, and each finished answer has its own Copy button. The header can make the panel larger (the choice is remembered), start a new chat or close it. If an answer finishes while the panel is closed, a dot appears on the bubble. The thread survives a page reload, and on a phone the panel fills the screen. A demo with the JavaScript API wired to buttons runs at `http://localhost:8081/demo`.

| Attribute | Default | Meaning |
|---|---|---|
| `data-provider` | server default | `chatgpt-browser` or `gemini-browser` |
| `data-title` | `Ask AI` | header text |
| `data-greeting` | a short hello | the first assistant line |
| `data-suggestions` | none | starter prompts shown as chips on an empty thread, separated by `\|`, for example `Summarise this page\|Explain this error` |
| `data-system` | none | a system prompt sent with every turn |
| `data-position` | `br` | `br` bottom right, `bl` bottom left |
| `data-theme` | `light` | `light`, `dark`, or `auto`, which follows the host page |
| `data-accent` | brand lavender | one colour for the bubble, send button and focus ring in both themes; the icon on it turns dark automatically when the colour is pale |
| `data-open` | closed | `1` starts it open |
| `data-persist` | `1` | `0` forgets the thread on reload |
| `data-attach` | `1` | `0` hides image attach |
| `data-key` | none | the API key, for a server that requires one |

The default accent changed in 2.1: the bubble and send button used to be black on light pages and white on dark ones, and are now the brand lavender in both. Set `data-accent` to pin any colour.

### JavaScript API

`window.BrowserLLMWidget` controls the widget from the host page:

| Call | What it does |
|---|---|
| `open()`, `close()` | open or close the panel |
| `reset()` | clear the thread (and any queued `ask()` calls, which reject with `reset`) |
| `ask(text, {open, images})` | send `text` as a user turn and return a Promise of the answer text. The panel opens unless `open: false`. `images` is an optional list of image URLs or data URLs. Calls made while an answer is streaming wait in a queue and run in order. The Promise rejects with an Error when the answer fails (the server's message), is stopped (`stopped`), or the thread is reset (`reset`). |
| `on(event, fn)` | listen for an event; returns a function that removes the listener |
| `off(event, fn)` | remove a listener |
| `config({...})` | change `title`, `accent`, `theme`, `position`, `greeting`, `suggestions` (array or `\|` string), `provider`, `system` or `attach` without reloading; the thread is kept |
| `destroy()` | take the widget off the page, so it can be added again with new attributes |

| Event | Payload | When |
|---|---|---|
| `ready` | `{base}` | the widget is on the page. A listener added later still gets it once. A `browserllm:ready` event with the API as `detail` also fires on `window`, for code that runs before the widget script. |
| `open`, `close` | none | the panel opens or closes |
| `message` | `{role, content}` | a user turn is sent (`role: "user"`) or an answer finishes (`role: "assistant"`) |
| `error` | `{message}` | an answer failed |

```html
<script src="http://localhost:8081/widget.js" data-suggestions="Explain this page|Draft a reply"></script>
<script>
  BrowserLLMWidget.on("message", m => console.log(m.role, m.content));
  document.querySelector("#help").onclick = () =>
    BrowserLLMWidget.ask("Explain the error on this page in two sentences.")
      .then(answer => console.log(answer))
      .catch(e => console.warn("no answer:", e.message));
</script>
```

Links in answers keep only `http(s)`, `mailto` and same-site addresses; anything else, such as a `javascript:` link a page could talk the model into writing, is replaced with `#`.

The widget inherits the server's trust model: anyone who can load a page with it can use your signed-in session. Keep it on a trusted network.

## Desktop app

`desktop/` holds a native GTK3 tray app for Linux, a thin client of the same API that runs on the system Python with the standard library only. Its tray popup makes image assets for the VS Code project you have focused and saves them into it, and a full window has Chat, Images, Gallery and Status tabs. Setup and details are in [desktop/README.md](../desktop/README.md).
