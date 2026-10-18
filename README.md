# fightingsleep plugins

A plugin marketplace that works with both **Claude Code** and **Codex**. Skills (`SKILL.md`) are shared; each tool gets its own manifests.

```
.claude-plugin/marketplace.json      # Claude Code catalog
.agents/plugins/marketplace.json     # Codex catalog
plugins/<name>/
  .claude-plugin/plugin.json         # Claude Code manifest
  .codex-plugin/plugin.json          # Codex manifest
  skills/<skill>/SKILL.md            # shared skills
```

## Install

**Claude Code**
```
/plugin marketplace add fightingsleep/plugin-marketplace
/plugin install boox@fightingsleep-plugins
```

**Codex**
```
codex plugin marketplace add fightingsleep/plugin-marketplace
```
Then open `/plugins` in Codex and install `boox`.

## Plugins

### boox

Lets your agent work with a [BOOX](https://www.boox.com/) e-ink tablet over the internet, through BOOX's Send2Boox cloud. The tablet doesn't need to be on the same network.

- **Send:** send files unchanged, or have the agent write a document (Markdown rendered to a clean e-ink PDF or EPUB).
- **Read handwriting:** render notebooks and annotated PDFs, with your ink, so the agent can read what you wrote.
- **Write in ink:** add replies to a notebook page as real pen strokes that you can erase like your own.
- **Sleep screen:** push an image, PDF or HTML page as the tablet's sleep-screen image.
- **Manage:** list and delete sent files.

**Requirements**
- [uv](https://docs.astral.sh/uv/), which installs Python and every dependency automatically. That's all: it works on Windows, macOS and Linux (including WSL) in Bash or PowerShell.

Documents (Markdown, text, DOCX, ODT, RST, Org, LaTeX) are converted with pandoc and rendered to PDF with [Typst](https://typst.app), both bundled. Notes:
- HTML with its own CSS (designed pages, HTML sleep screens) is rendered by an installed Chrome, Edge or Chromium (Edge comes with Windows), or by WeasyPrint where Pango is installed. It's rendered offline and without scripts, and can only use images and fonts from its own folder.
- CJK text needs a CJK font, which Windows and macOS include. On Linux, install one such as Noto Sans CJK; without it, CJK documents are sent as EPUB.
- Not supported: Alpine/musl Linux, where Typst has no prebuilt package. On Windows on ARM, documents need a [pandoc](https://pandoc.org/installing.html) install and are rendered with Edge.

Run `send2boox.py doctor` to see what a machine will use.

**Sign in** by asking your agent to log in to your Boox, or run:
```
uv run --script plugins/boox/skills/send-to-boox/scripts/send2boox.py login-request you@example.com
uv run --script plugins/boox/skills/send-to-boox/scripts/send2boox.py login 123456
```
For accounts on the EU server, add `--cloud eur.boox.com` to `login-request`.

Built and tested on a BOOX Note Max. Other BOOX tablets that support Send2Boox should work; pass `--screen WxH` for sleep screens on other models.

## Adding a plugin

1. Create `plugins/<name>/` with `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json` and `skills/<skill>/SKILL.md`.
2. Add an entry to **both** marketplace files.

## Disclaimer

The `boox` plugin is an independent, unofficial project. It is not affiliated with, endorsed by, or supported by Onyx International (BOOX). It talks to BOOX's cloud service (Send2Boox, `push.boox.com`) through undocumented endpoints that may change or stop working at any time, and its use may be subject to BOOX's terms of service. Files you send, and notebook data the plugin reads or writes, go through BOOX's cloud storage. Use it with your own account, at your own risk. Your login token is stored locally in `~/.config/send2boox/config.json`.

## License

[MIT](LICENSE)
