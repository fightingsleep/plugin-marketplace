---
name: send-to-boox
description: Work with the user's BOOX (Onyx) e-ink tablet over the internet via Send2Boox - send files or generated documents to it, read the handwriting in its notebooks and annotated PDFs, write replies back as real pen strokes, set its sleep-screen image, and list or delete sent files. Use when the user mentions their Boox, e-ink tablet or e-reader, e.g. "send this to my Boox", "put it on my tablet", "what did I write in my notebook", "reply on my tablet", or "set my sleep screen".
---

# Send to BOOX

Transfers files to the user's BOOX tablet through the Send2Boox cloud. It works from anywhere, and the tablet doesn't need to be on the same network. Files download automatically to the tablet within a few seconds while it's online.

The CLI is `scripts/send2boox.py` in this skill's directory. Run it with `uv run --script <skill-dir>/scripts/send2boox.py ...` on Windows, macOS and Linux, in Bash or PowerShell; quote the path if it has spaces. uv installs its dependencies automatically, and nothing else needs installing. If a conversion fails or falls back, `send2boox.py doctor` shows which renderers this machine has.

## Sending files: send them unchanged

The user is transferring their file, so don't convert, rename or modify it unless they ask.

- `send2boox.py send report.pdf notes.docx ...` sends files exactly as they are.
- `--title "Name"` sets the name shown on the tablet (single file only). Only use it if the user wants a different name.
- Convert only when asked, e.g. "send this markdown as a PDF": `send2boox.py send notes.md --as pdf` (or `--as epub`).

## Sending content you wrote

When there's no file (a summary, notes or a report you generated), use `send-text`. It turns Markdown into a clean, e-ink-friendly PDF by default. Write the Markdown to a temporary file, then send it:

`send2boox.py send-text --file notes.md --title "Reading notes"`

- `--file` works the same in every shell. Prefer it on Windows: PowerShell can garble non-ASCII text that's piped in. In Bash you can also pipe: `printf '%s' "$CONTENT" | send2boox.py send-text --title "Reading notes"`.
- Write the content as Markdown. Headings, lists, tables, code blocks, footnotes and math all render well. Images must be local files next to the Markdown file; remote images are replaced by their alt text.
- Use `--as epub` for long prose meant to be read like a book, since EPUB text reflows and can be resized.
- Always give a short, descriptive `--title`, because that's the name the user sees on the tablet.

Format ranking for a stylus tablet such as the Note Max (13.3", 3:4): **PDF** (best: near print-size pages that the user can annotate) > **EPUB** (long reflowable reading) > **TXT** (fallback only). The script falls back down this list automatically if a conversion fails, for example when no CJK font is installed to render a PDF. The last line of output says what was sent: `Sent: Notes.pdf`, or `Sent as EPUB (PDF unavailable: <reason>): Notes.epub`. When it falls back, tell the user what was sent and why.

## Reading the user's handwriting

The user can write with the stylus on any document you send, and in their notebooks. That ink syncs back to BOOX's cloud, usually within a minute when the tablet is online.

- `send2boox.py ink` lists notebooks and annotated documents, most recently changed first.
- `send2boox.py ink "My Notebook" --out DIR` renders every page with the user's ink drawn in red on the original page, and saves one PNG per page. **Look at the PNGs** to read what they wrote and where. Erased strokes are left out.
- If nothing new shows up, the tablet hasn't synced yet. Ask the user to sync notes rather than polling in a loop.
- Never print the user's notebook contents unless they asked you to read them.

## Writing back in ink

`send2boox.py write NOTEBOOK_ID "Your reply" --page 2 --x 0.13 --y 0.80` writes the text in a handwriting-style script as **real pen strokes**. They appear on the tablet at its next note sync, and the user can erase or move them like their own ink.

- Positions and sizes are fractions of the page. Render the page with `ink` first, choose a clear spot (for example the ruled line below the user's note), write, then render again to check it doesn't overlap anything.
- `--size` is the letter height (default 0.022 of the page height). Text wraps at `--width`.
- Only write into notebooks or documents the user asked you to reply in, and keep replies short.
- Pass the notebook **ID** from `ink`, not a title. Before writing, tell the user which notebook (title and ID) and page you'll write on, unless they've already said exactly where.
- The handwriting font covers ASCII text only.

## Sleep screen

`send2boox.py screensaver FILE --title NAME` pushes an image, PDF or HTML page as the tablet's sleep-screen image. It's fitted to the full screen (default 2400×3200, the Note Max; pass `--screen WxH` for other models) and shows whenever the tablet sleeps. Use `--preview out.png` to check the render first. In HTML, `<ink size="22" width="470">text</ink>` renders as handwriting.

## Managing sent files

- `send2boox.py list` shows recently sent files with their IDs.
- `send2boox.py delete ID [ID ...]` deletes sent files. Confirm with the user before deleting, and only delete what they asked you to remove. It refuses notebook IDs, and copies the tablet already downloaded may stay on it.

## Not logged in

If a command fails with `not logged in` or an auth error, don't guess credentials. Ask the user to run:
1. `send2boox.py login-request THEIR_EMAIL` (add `--cloud eur.boox.com` if their account is on the EU server)
2. `send2boox.py login CODE` with the 6-digit code from their email

The token is stored in `~/.config/send2boox/config.json` (readable only by the user). Never print or echo it.

## Notes

- This uses an unofficial, reverse-engineered BOOX API. The script already retries when the BOOX server is slow. If a command still fails, report the exact error instead of retrying in a loop.
- Sending can take up to a minute while BOOX processes the file.
- Only send what the user asked for. Everything sent is uploaded to BOOX's cloud.
- HTML with its own CSS (`send page.html --as pdf`, HTML sleep screens) is rendered by an installed Chrome, Edge or Chromium (Edge comes with Windows), or by WeasyPrint where its libraries exist. It's rendered offline and without scripts, and can only load non-hidden files from its own folder. Still, don't render HTML from untrusted sources. The script refuses HTML that sits directly in the home folder or a drive root.
