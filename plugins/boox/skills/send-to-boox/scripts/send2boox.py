#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.31",
#   "alibabacloud-oss-v2>=1.4",
#   "pypandoc_binary>=1.13; sys_platform != 'win32' or platform_machine != 'ARM64'",
#   "pypandoc>=1.13; sys_platform == 'win32' and platform_machine == 'ARM64'",
#   "typst>=0.15; sys_platform != 'win32' or platform_machine != 'ARM64'",
#   "weasyprint>=62; sys_platform != 'win32'",
#   "pypdfium2>=4",
#   "pillow>=10",
#   "Hershey-Fonts>=2",
# ]
# ///
"""Send files to a BOOX e-ink tablet through the Send2Boox cloud service.

Unofficial: uses the same endpoints as the push.boox.com web app
(see https://github.com/hrw/onyx-send2boox). They may change without notice.

`send` transfers files unchanged. Conversion only happens for `send-text` (content with
no original file) or when asked for with `send --as pdf|epub`. Formats are ranked for a
BOOX Note Max (13.3" 3:4 e-ink, stylus annotation in NeoReader):
  1. PDF  - fixed layout at near print size; tables/code/images survive; annotatable.
  2. EPUB - reflowable; better for long prose (books, long articles).
  3. TXT  - fallback only, if PDF/EPUB conversion fails.

Rendering works on Windows, macOS and Linux with nothing to install but uv: documents go
through pandoc and Typst (both bundled). HTML with its own CSS (designed pages, sleep
screens) needs an HTML engine: an installed Chrome, Edge or Chromium, or WeasyPrint where
its Pango libraries exist. `send2boox.py doctor` shows what's available.

Config: ~/.config/send2boox/config.json (mode 600), or BOOX_TOKEN / BOOX_CLOUD env vars.
"""
import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from functools import cache, cached_property
from html import escape, unescape
from pathlib import Path

import requests

CONFIG_PATH = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "send2boox" / "config.json"
DEFAULT_CLOUD = "push.boox.com"

# Sent as-is: formats NeoReader renders natively and well.
NATIVE = {".pdf", ".epub", ".djvu", ".cbz", ".cbr", ".mobi", ".azw3", ".fb2",
          ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
# Converted (pandoc input format by extension).
CONVERTIBLE = {".md": "markdown", ".markdown": "markdown", ".txt": "markdown", ".html": "html",
               ".htm": "html", ".docx": "docx", ".odt": "odt", ".rst": "rst", ".org": "org", ".tex": "latex"}

# The same look as EINK_CSS, for Typst: pure black on white, a 3:4 page, print-size type.
# Body and code use fonts embedded in Typst, so documents look the same on every OS;
# headings use a common system sans. Typst falls back to any installed font for missing glyphs.
TYPST_PREAMBLE = """
#set page(width: 210mm, height: 280mm, margin: (top: 14mm, x: 15mm, bottom: 16mm),
  footer: context align(center, text(9pt, counter(page).display("1 / 1", both: true))))
#set text(font: "Libertinus Serif", size: 13pt, fill: black)
#set par(leading: 0.8em, spacing: 1.2em)
#show heading: set text(font: ("DejaVu Sans", "Noto Sans", "Segoe UI", "Helvetica Neue", "Arial"), weight: "bold")
#show heading: set block(above: 1.4em, below: 0.7em)
#show heading.where(level: 1): set text(22pt)
#show heading.where(level: 2): set text(16pt)
#show heading.where(level: 2): it => block(width: 100%, stroke: (bottom: 1.5pt), inset: (bottom: 3pt), it)
#show heading.where(level: 3): set text(13pt)
#show heading.where(level: 4): set text(12pt)
#show link: underline
#show raw: set text(font: "DejaVu Sans Mono", size: 10pt)
#show raw.where(block: true): it => block(width: 100%, stroke: 1pt, inset: (x: 8pt, y: 6pt), it)
#show quote.where(block: true): it => block(stroke: (left: 3pt), inset: (left: 10pt, y: 2pt),
  text(style: "italic", it.body))
#show table: set align(left)
#set table(stroke: 1pt, inset: (x: 6pt, y: 4pt))
#set table.hline(stroke: 2pt)
#show table.cell.where(y: 0): set text(weight: "bold")
#show figure: set block(breakable: true)
#set figure(numbering: none)
#show figure.caption: set text(10pt)
#let horizontalrule = block(above: 1em, below: 1em, line(length: 100%, stroke: 1pt))
"""

# Tuned for e-ink: pure black on white (grey fills dither), 3:4 page matching the
# Note Max's 2400x3200 screen so pages display without letterboxing, print-size type.
EINK_CSS = """
@page { size: 210mm 280mm; margin: 14mm 15mm 16mm;
        @bottom-center { content: counter(page) " / " counter(pages); font-size: 9pt; } }
html { font-family: "DejaVu Serif", "Noto Serif", Georgia, serif; font-size: 12pt; line-height: 1.5; color: #000; }
h1, h2, h3, h4 { font-family: "DejaVu Sans", "Noto Sans", Helvetica, sans-serif; line-height: 1.2;
                 page-break-after: avoid; margin: 1.2em 0 0.4em; }
h1 { font-size: 22pt; } h2 { font-size: 16pt; border-bottom: 1.5pt solid #000; padding-bottom: 2pt; }
h3 { font-size: 13pt; }
p, li { orphans: 3; widows: 3; }
a { color: #000; }
pre, code { font-family: "DejaVu Sans Mono", "Noto Sans Mono", monospace; font-size: 10pt; }
pre { border: 1pt solid #000; padding: 6pt 8pt; white-space: pre-wrap; page-break-inside: avoid; }
blockquote { margin-left: 0; padding-left: 10pt; border-left: 3pt solid #000; font-style: italic; }
table { border-collapse: collapse; width: 100%; page-break-inside: avoid; margin: 0.8em 0; }
th, td { border: 1pt solid #000; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { font-weight: bold; border-bottom: 2pt solid #000; }
img { max-width: 100%; }
"""


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    if os.environ.get("BOOX_TOKEN"):
        cfg["token"] = os.environ["BOOX_TOKEN"]
    if os.environ.get("BOOX_CLOUD"):
        cfg["cloud"] = os.environ["BOOX_CLOUD"]
    cfg.setdefault("cloud", DEFAULT_CLOUD)
    return cfg


def check_cloud(host: str) -> None:
    """The login token is sent to this host, so only accept BOOX's own servers."""
    if host != "boox.com" and not host.endswith(".boox.com"):
        sys.exit(f"error: '{host}' is not a boox.com server (e.g. push.boox.com, eur.boox.com)")


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_PATH.parent, 0o700)
    fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if hasattr(os, "fchmod"):  # missing on Windows before Python 3.13, where the profile's ACLs protect it
        os.fchmod(fd, 0o600)  # O_CREAT's mode doesn't apply to an existing file
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def sg_request(s: requests.Session, method: str, url: str, **kw) -> requests.Response | None:
    """Sync Gateway requests sometimes hang for a while, so use short timeouts and retry."""
    import time

    r = None
    for _ in range(8):
        try:
            r = s.request(method, url, timeout=10, **kw)
            if r.status_code not in (502, 503, 504):
                return r
        except requests.RequestException:
            pass
        time.sleep(5)
    return r


def ts(d: dict) -> float:
    """A record's updatedAt (or createdAt) in epoch seconds: it's epoch ms or an ISO string."""
    from datetime import datetime

    v = d.get("updatedAt") or d.get("createdAt") or 0
    if isinstance(v, (int, float)):
        return v / 1000
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def no_attachments(html: str) -> str:
    """Drop rel=attachment so WeasyPrint can't embed files into an uploaded PDF."""
    return re.sub(r"""\brel\s*=\s*(["']?)[^"'>]*\battachment\b[^"'>]*\1""", "", html, flags=re.I)


def resource_root(base: Path) -> Path:
    """The folder an HTML page may load images and fonts from: its own, but never a home or drive root."""
    root = base.resolve()
    if root == Path.home().resolve() or root == Path(root.anchor):
        raise RenderError(f"won't render HTML whose folder is {root}; move it into its own folder")
    return root


def allowed_file(root: Path, path: Path) -> bool:
    """True for a regular file under root that isn't hidden (no dotfile or dot-folder like .ssh)."""
    p = path.resolve()
    return p.is_relative_to(root) and p.is_file() and not any(s.startswith(".") for s in p.relative_to(root).parts)


def local_fetcher(base: Path):
    """WeasyPrint URL fetcher that only loads data: URIs and files under `base`.

    Rendered pages are uploaded to the cloud, so HTML must not be able to pull in
    (or attach) arbitrary local files such as ~/.ssh keys.
    """
    from urllib.parse import urlparse
    from urllib.request import url2pathname

    root = resource_root(base)

    def check(url):
        u = urlparse(url)
        if not (u.scheme == "data" or (u.scheme == "file" and allowed_file(root, Path(url2pathname(u.path))))):
            raise ValueError(f"blocked resource: {url}")

    try:  # WeasyPrint 68+
        from weasyprint.urls import URLFetcher

        class Fetcher(URLFetcher):
            def fetch(self, url, *args, **kw):
                check(url)
                return super().fetch(url, *args, **kw)

        return Fetcher()
    except ImportError:
        from weasyprint import default_url_fetcher

        def fetch(url, *args, **kw):
            check(url)
            return default_url_fetcher(url, *args, **kw)

        return fetch


class Boox:
    def __init__(self, cloud: str, token: str | None = None):
        self.cloud, self.token = cloud, token

    # Looked up once per run, so batch sends don't repeat the setup calls.
    @cached_property
    def uid(self) -> str:
        return self.call("users/me")["data"]["uid"]

    @cached_property
    def storage(self) -> "Storage":
        return Storage(self.call("config/buckets")["data"]["onyx-cloud"], self.call("config/stss")["data"])

    def call(self, path: str, data: dict | None = None, params: dict | None = None, timeout: int = 30) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        url = f"https://{self.cloud}/api/1/{path}"
        try:
            if data is not None:
                r = requests.post(url, json=data, headers=headers, timeout=timeout)
            else:
                r = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            sys.exit(f"error: {path}: {e}")
        try:
            body = r.json()
        except ValueError:
            sys.exit(f"error: {path}: HTTP {r.status_code}, non-JSON response")
        if r.status_code >= 400 or body.get("result_code", 0) != 0:
            msg = body.get("message") or body.get("msg") or body
            sys.exit(f"error: {path}: HTTP {r.status_code}: {msg}")
        return body

    def send_file(self, path: Path, title: str | None = None) -> str:
        key = f"{self.uid}/push/{uuid.uuid4()}{path.suffix}"
        self.storage.upload(key, path)
        name = title or path.name
        if not Path(name).suffix:
            name += path.suffix
        # saveAndPush is slow (often 30s+) because the server processes the file before replying.
        saved = self.call("push/saveAndPush", timeout=180, data={"data": {
            "bucket": self.storage.bucket,
            "name": name,
            "parent": None,
            "resourceDisplayName": name,
            "resourceKey": key,
            "resourceType": path.suffix.lstrip(".").lower() or "txt",
            "title": name,
        }})
        self.repush(saved["data"]["_id"])
        return name

    @cached_property
    def sync(self) -> tuple[requests.Session, str]:
        """Session for the Couchbase Sync Gateway that BOOX devices sync push records from."""
        st = self.call("users/syncToken")["data"]
        s = requests.Session()
        s.cookies.set(st["cookie_name"], st["session_id"])
        return s, f"https://{self.cloud}/neocloud"

    def delete(self, ids: list[str]) -> None:
        """Delete pushed files the way the website does: remove the sync record, then the cloud entry."""
        s, db = self.sync
        revs = {}
        for i in ids:  # check every ID first, so nothing is deleted if one is wrong
            r = sg_request(s, "GET", f"{db}/{i}")
            if r is not None and r.status_code == 404:
                sys.exit(f"error: no sent file with ID {i} (see `list`); nothing deleted")
            if r is None or r.status_code != 200:
                sys.exit(f"error: BOOX server error looking up {i}; nothing deleted, try again")
            d = r.json()
            # Notebooks share this database: never delete anything but sent files.
            if d.get("contentType") not in ("digital_content", "push_screensaver") or d.get("dbId") not in (None, f"{self.uid}-MESSAGE"):
                sys.exit(f"error: {i} is not a sent file (it may be a notebook); nothing deleted")
            revs[i] = d["_rev"]
        for i, rev in revs.items():
            sg_request(s, "POST", f"{db}/_bulk_docs", json={"docs": [{"_id": i, "_rev": rev, "_deleted": True}]})
        self.call("push/message/batchDelete", data={"ids": ids})

    def repush(self, doc_id: str) -> None:
        """Touch the push record in the sync gateway so the tablet downloads it.

        saveAndPush records the file, but the tablet only fetches it when the record
        changes. This does what the website's "Re-push" button does: bump updatedAt.
        """
        import time

        s, db = self.sync
        doc = None
        deadline = time.time() + 120  # the record can take a while to appear
        while time.time() < deadline:
            r = sg_request(s, "GET", f"{db}/{doc_id}")
            if r is not None and r.status_code == 200:
                doc = r.json()
                break
            time.sleep(2)
        if not doc:
            sys.exit("error: file uploaded but its push record wasn't found; use Re-push on push.boox.com")
        content = json.loads(doc["content"]) if isinstance(doc["content"], str) else doc["content"]
        doc["updatedAt"] = content["updatedAt"] = int(time.time() * 1000)
        doc["content"] = json.dumps(content)
        # Use _bulk_docs, as the web app's replication does; a plain PUT hangs on this server.
        r = sg_request(s, "POST", f"{db}/_bulk_docs", json={"docs": [doc]})
        if r is None:
            sys.exit("error: file uploaded but the tablet wasn't notified; use Re-push on push.boox.com")
        result = (r.json() or [{}])[0] if r.ok else {}
        # conflict = an earlier timed-out attempt already landed
        if not (result.get("rev") or result.get("error") == "conflict"):
            sys.exit(f"error: re-push failed: HTTP {r.status_code} {r.text[:200]}")


class Storage:
    """BOOX's Aliyun OSS bucket, accessed with the account's temporary (STS) credentials."""

    def __init__(self, cfg: dict, sts: dict):
        import alibabacloud_oss_v2 as oss

        host = cfg["aliEndpoint"].split("://")[-1].strip("/")  # e.g. oss-us-west-1.aliyuncs.com
        c = oss.config.load_default()
        c.credentials_provider = oss.credentials.StaticCredentialsProvider(
            sts["AccessKeyId"], sts["AccessKeySecret"], sts["SecurityToken"])
        c.endpoint = host
        c.region = (cfg.get("region") or host.split(".")[0]).removeprefix("oss-")
        self.oss, self.client, self.bucket = oss, oss.Client(c), cfg["bucket"]

    def upload(self, key: str, path: Path) -> None:
        # Large files go up in parts; no checkpoint files are left behind.
        self.client.uploader().upload_file(self.oss.PutObjectRequest(bucket=self.bucket, key=key), filepath=str(path))

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(self.oss.PutObjectRequest(bucket=self.bucket, key=key, body=data))

    def get(self, key: str) -> bytes:
        return self.client.get_object(self.oss.GetObjectRequest(bucket=self.bucket, key=key)).body.read()

    def list(self, prefix: str) -> list:
        """Objects (with .key and .last_modified) under a prefix."""
        pages = self.client.list_objects_v2_paginator().iter_page(
            self.oss.ListObjectsV2Request(bucket=self.bucket, prefix=prefix))
        return [o for page in pages for o in page.contents or []]

    def sign_url(self, key: str, seconds: int) -> str:
        from datetime import timedelta

        req = self.oss.GetObjectRequest(bucket=self.bucket, key=key, response_content_disposition="attachment")
        return self.client.presign(req, expires=timedelta(seconds=seconds)).url


def client(require_token: bool = True) -> Boox:
    cfg = load_config()
    if require_token and not cfg.get("token"):
        sys.exit("error: not logged in. Run: send2boox.py login-request EMAIL, then send2boox.py login CODE")
    check_cloud(cfg["cloud"])
    return Boox(cfg["cloud"], cfg.get("token"))


def cmd_login_request(a):
    cfg = load_config()
    cfg["cloud"], cfg["email"] = a.cloud or cfg["cloud"], a.email
    check_cloud(cfg["cloud"])
    Boox(cfg["cloud"]).call("users/sendMobileCode", data={"mobi": a.email})
    cfg.pop("token", None)
    save_config(cfg)
    print(f"Verification code sent to {a.email} (server {cfg['cloud']}). Run: send2boox.py login CODE")


def cmd_login(a):
    cfg = load_config()
    if not cfg.get("email"):
        sys.exit("error: run login-request first")
    check_cloud(cfg["cloud"])
    body = Boox(cfg["cloud"]).call("users/signupByPhoneOrEmail", data={"mobi": cfg["email"], "code": a.code})
    cfg["token"] = body["data"]["token"]
    save_config(cfg)
    print(f"Logged in. Token saved to {CONFIG_PATH}")


def cmd_status(a):
    me = client().call("users/me")["data"]
    print(f"Logged in as {me.get('nickname') or me.get('email') or me.get('uid')} on {load_config()['cloud']}")


class RenderError(Exception):
    """A renderer couldn't produce a file. The message is a short, one-line reason."""


def reason(e: Exception) -> str:
    """One line from an exception, for warnings and the final "Sent as" line."""
    msg = (getattr(e, "message", None) or str(e)).strip()
    return (msg.splitlines()[0] if msg else type(e).__name__)[:200]


CJK_TEXT = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")
# Font families that cover CJK, across Windows, macOS and Linux.
CJK_FONTS = re.compile(
    r"CJK|Source Han|WenQuanYi|Droid Sans Fallback|AR PL|YaHei|JhengHei|SimSun|SimHei|MingLiU|KaiTi|FangSong|"
    r"Yu Gothic|Yu Mincho|MS Gothic|MS Mincho|Meiryo|Malgun|Gulim|Batang|Dotum|PingFang|Hiragino|Heiti|Songti|"
    r"Apple SD Gothic|AppleGothic|Nanum|Noto (Sans|Serif) (SC|TC|HK|JP|KR)\b|Sarasa|LXGW|IPA(ex)?(Gothic|Mincho)|"
    r"Takao|Baekmuk|UnDotum", re.I)


@cache
def cjk_font_installed() -> bool:
    """True if some installed font covers CJK.

    Without one a PDF would show boxes, so CJK documents go as EPUB instead: the tablet
    renders EPUB with its own fonts.
    """
    try:
        import typst

        return any(CJK_FONTS.search(f) for f in typst.Fonts().families())
    except ImportError:
        if not shutil.which("fc-list"):
            return True  # can't tell; Windows and macOS ship CJK fonts
        import subprocess

        return bool(subprocess.run(["fc-list", ":lang=zh", "family"], capture_output=True, text=True).stdout.strip())


def read_doc(src: Path, fmt: str, build: Path) -> dict:
    """Parse a document into pandoc's AST, sandboxed so the reader can't include other files
    or fetch URLs. Images embedded in DOCX/ODT are extracted into build/media."""
    import pypandoc

    args = ["--sandbox", "--quiet"]
    if fmt in ("docx", "odt"):
        args.append(f"--extract-media={build / 'media'}")
    return json.loads(pypandoc.convert_file(str(src), "json", format=fmt, extra_args=args))


def write_doc(ast: dict, to: str, *args: str, outputfile: Path | None = None) -> str:
    import pypandoc

    return pypandoc.convert_text(json.dumps(ast), to, format="json", extra_args=["--quiet", *args],
                                 outputfile=str(outputfile) if outputfile else None)


TYPST_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
SAFE_META = {"title", "subtitle", "author", "date", "lang", "abstract", "description", "subject", "keywords"}
_DROP = object()


def sanitize(ast: dict, roots: list[Path], build: Path) -> dict:
    """Make a document safe and self-contained to render and upload.

    Images under `roots` (the document's folder, extracted media) are copied into `build`,
    as PNG if Typst can't read their format, and data: images are decoded. Any other image
    (remote, missing, hidden or outside) becomes its alt text: nothing is fetched, no other
    local file ends up in the upload, and one bad image can't fail the whole document.
    Raw HTML/Typst/LaTeX is dropped (pandoc's EPUB writer would embed files that raw
    <img> tags point to), and code languages too (syntax colours dither on e-ink).
    """
    import base64
    from urllib.parse import unquote, urlparse
    from urllib.request import url2pathname

    from PIL import Image

    def usable_root(r: Path) -> Path | None:
        try:
            return resource_root(r)
        except RenderError:  # a home folder or drive root: don't embed images from all of it
            return None

    base = roots[0].resolve()
    roots = [r for r in map(usable_root, roots) if r] + [build.resolve()]
    count = 0

    def image(url: str) -> str | None:
        nonlocal count
        count += 1
        name = f"image{count}"
        try:
            if url.startswith("data:"):
                m = re.match(r"data:image/(png|jpe?g|gif|webp|svg\+xml);base64,(.*)$", url, re.S | re.I)
                if not m:
                    return None
                ext = {"svg+xml": ".svg", "jpeg": ".jpg"}.get(m.group(1).lower(), "." + m.group(1).lower())
                path = build / (name + "-data" + ext)
                path.write_bytes(base64.b64decode(m.group(2)))
            elif re.match(r"[A-Za-z]:[\\/]", url):  # a Windows path, not a URL scheme
                path = Path(url)
            else:
                u = urlparse(url)
                if u.scheme not in ("", "file") or u.netloc:
                    return None
                path = Path(url2pathname(u.path) if u.scheme == "file" else unquote(u.path))
            if not path.is_absolute():
                path = base / path
            if not any(allowed_file(r, path) for r in roots):
                return None
            ext = path.suffix.lower()
            if ext in TYPST_IMAGES:
                if ext == ".svg":
                    # An SVG that links other files (rather than embedding them) fails the whole document in Typst.
                    if re.search(r"""href\s*=\s*["'](?!data:|#)""", path.read_text(encoding="utf-8", errors="replace")):
                        return None
                else:
                    with Image.open(path) as im:  # a corrupt image would fail the whole document
                        im.verify()
                shutil.copyfile(path, build / (name + ext))
                return name + ext
            with Image.open(path) as im:  # e.g. BMP or TIFF
                im.save(build / f"{name}.png")
            return f"{name}.png"
        except Exception:
            return None

    def walk(x):
        if isinstance(x, list):
            return [y for y in map(walk, x) if y is not _DROP]
        if not isinstance(x, dict) or "t" not in x:
            return x
        t, c = x["t"], x.get("c")
        if t in ("RawBlock", "RawInline"):
            return _DROP
        if t in ("CodeBlock", "Code"):
            c[0][1] = []
        if t == "Image":
            src = image(c[2][0])
            if src is None:
                return {"t": "Span", "c": [["", [], []], walk(c[1])]}
            c[2][0] = src
        if c is not None:
            x["c"] = walk(c)
        return x

    ast["blocks"] = walk(ast["blocks"])
    # Metadata such as cover-image or css names files that pandoc's EPUB writer embeds.
    ast["meta"] = {k: walk(v) for k, v in ast.get("meta", {}).items() if k in SAFE_META}
    return ast


def typst_str(s: str) -> str:
    return '"' + re.sub(r"[\x00-\x1f]", " ", s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def typst_pdf(ast: dict, build: Path, title: str, out: Path) -> None:
    """Render a document with Typst. Its root is the build folder, so it can't read anything else."""
    import warnings

    try:
        import typst
    except ImportError:
        raise RenderError("Typst isn't available on this platform") from None
    main = build / "main.typ"
    main.write_text(f"#set document(title: {typst_str(title)})\n{TYPST_PREAMBLE}\n{write_doc(ast, 'typst')}",
                    encoding="utf-8")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. "unknown font family" for fonts of other platforms
            out.write_bytes(typst.compile(str(main), root=str(build)))
    except Exception as e:
        raise RenderError(f"Typst: {reason(e)}") from None


def html_page(body: str, title: str) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>'
            f"<style>{EINK_CSS}</style></head><body>{body}</body></html>")


@cache
def weasyprint():
    """The weasyprint module, or why it can't be used. Its install banner is kept off the output."""
    import contextlib
    import logging

    logging.getLogger("weasyprint").setLevel(logging.ERROR)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            import weasyprint
        return weasyprint
    except ImportError:
        return "WeasyPrint isn't installed on this platform"
    except OSError:
        return "WeasyPrint's Pango libraries aren't installed"


def find_browser() -> str | None:
    """An installed Chromium-based browser (Edge, Chrome, Chromium, Brave), or BOOX_BROWSER."""
    if os.environ.get("BOOX_BROWSER"):
        return shutil.which(os.environ["BOOX_BROWSER"])
    found = [shutil.which(n) for n in ("msedge", "chrome", "google-chrome", "google-chrome-stable", "chromium",
                                       "chromium-browser", "microsoft-edge", "microsoft-edge-stable", "brave-browser")]
    if sys.platform == "win32":
        for var in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            if base := os.environ.get(var):
                found += [str(Path(base, p)) for p in (r"Microsoft\Edge\Application\msedge.exe",
                          r"Google\Chrome\Application\chrome.exe", r"Chromium\Application\chrome.exe",
                          r"BraveSoftware\Brave-Browser\Application\brave.exe")]
    elif sys.platform == "darwin":
        for apps in ("/Applications", str(Path.home() / "Applications")):
            found += [f"{apps}/{a}.app/Contents/MacOS/{a}" for a in ("Google Chrome", "Microsoft Edge", "Chromium",
                                                                     "Brave Browser")]

    def usable(p: str) -> bool:
        # Snap browsers (Ubuntu's chromium) have a private /tmp, so they can't write the PDF where we look.
        if not Path(p).is_file() or "/snap/" in p or os.path.realpath(p).endswith("/snap"):
            return False
        with open(p, "rb") as f:
            head = f.read(4096)
        return not (head.startswith(b"#!") and b"/snap/" in head)  # a wrapper script that runs the snap

    return next((p for p in found if p and usable(p)), None)


def browser_pdf(html: str, base: Path, out: Path) -> None:
    """Print HTML to PDF with a headless Chromium-based browser, offline and sandboxed.

    The page comes from a throwaway localhost server that only serves non-hidden files under
    its folder, and every response carries a CSP that blocks scripts, frames and every other
    origin, so the page can't read other local files or reach the network.
    """
    import secrets
    import subprocess
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mimetypes import guess_type
    from urllib.parse import unquote, urlparse

    exe = find_browser()
    if not exe:
        if os.environ.get("BOOX_BROWSER"):
            raise RenderError(f"BOOX_BROWSER ({os.environ['BOOX_BROWSER']}) isn't an executable")
        raise RenderError("no Chrome, Edge or Chromium found")
    root, token = resource_root(base), secrets.token_urlsafe(16)
    # Drop <meta http-equiv> (refresh would navigate away; CSP doesn't stop that). Entities are
    # decoded first, since browsers decode them in attribute values.
    page = re.sub(r"<meta\b[^>]*>", lambda m: "" if "http-equiv" in unescape(m.group(0)).lower() else m.group(0),
                  html, flags=re.I)
    # WeasyPrint's default page, so both engines lay out pages alike; the page's own @page wins.
    page = re.sub(r"^\s*(<!doctype[^>]*>)?", lambda m: m.group(0) + "<style>@page { size: A4; margin: 75px }</style>",
                  page, count=1, flags=re.I).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path, body, ctype = unquote(urlparse(self.path).path), None, "text/plain"
            prefix = f"/{token}/"
            if path == prefix:
                body, ctype = page, "text/html; charset=utf-8"
            elif path.startswith(prefix) and allowed_file(root, root / path[len(prefix):]):
                f = root / path[len(prefix):]
                body, ctype = f.read_bytes(), guess_type(f.name)[0] or "application/octet-stream"
            self.send_response(200 if body is not None else 404)
            for k, v in (("Content-Security-Policy", csp), ("X-Content-Type-Options", "nosniff"),
                         ("Content-Type", ctype), ("Content-Length", str(len(body or b"")))):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    origin = f"http://127.0.0.1:{srv.server_address[1]}"
    csp = (f"default-src 'none'; style-src 'unsafe-inline' {origin} data:; img-src {origin} data:; "
           f"font-src {origin} data:; base-uri 'none'; form-action 'none'")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # A fresh profile that the browser creates itself: on Windows, a folder from mkdtemp
    # (owner-only ACL since Python 3.13) is unusable by Edge's sandboxed processes.
    profile = Path(tempfile.gettempdir()) / f"send2boox-{secrets.token_hex(8)}"
    pdf, proc = profile / "page.pdf", None
    args = [exe, "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--disable-sync", "--use-mock-keychain", "--password-store=basic",
            f"--user-data-dir={profile}", "--no-pdf-header-footer", f"--print-to-pdf={pdf}",
            # No network: names don't resolve, and anything else goes to a dead proxy
            # (loopback, where the page is served, always bypasses proxies).
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1", "--proxy-server=127.0.0.1:9",
            origin + f"/{token}/"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")  # Chromium won't run as root otherwise (containers)
    try:
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError as e:
            raise RenderError(f"couldn't start {Path(exe).stem}: {reason(e)}") from None
        # Edge's launcher can exit before the PDF is written, so wait for a complete file.
        deadline, exited = time.time() + 90, None
        while time.time() < deadline:
            try:
                if pdf.is_file() and pdf.read_bytes().rstrip().endswith(b"%%EOF"):
                    shutil.copyfile(pdf, out)
                    return
            except OSError:  # still being written (Windows locks it)
                pass
            if proc.poll() is not None:
                exited = exited or time.time()
                if time.time() - exited > 10:
                    break
            time.sleep(0.2)
        raise RenderError(f"{Path(exe).stem} didn't produce a PDF")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        srv.shutdown()
        srv.server_close()
        for _ in range(10):  # the browser may still be shutting down and holding files
            shutil.rmtree(profile, ignore_errors=True)
            if not profile.exists():
                break
            time.sleep(0.5)


def html_pdf(html: str, base: Path, out: Path) -> None:
    """Render HTML that has its own CSS to PDF.

    Uses an installed Chromium-based browser when there is one (the same engine on every
    OS), else WeasyPrint.
    """
    resource_root(base)
    reasons = []
    try:
        return browser_pdf(html, base, out)
    except RenderError as e:
        reasons.append(str(e))
    wp = weasyprint()
    if isinstance(wp, str):
        reasons.append(wp)
    else:
        try:
            return wp.HTML(string=no_attachments(html), base_url=str(base),
                           url_fetcher=local_fetcher(base)).write_pdf(out)
        except Exception as e:
            reasons.append(f"WeasyPrint: {reason(e)}")
    raise RenderError("; ".join(reasons))


def convert(src: Path, target: str, title: str, outdir: Path) -> tuple[Path, str | None]:
    """Convert a document to PDF or EPUB for the tablet, falling back down the ranking.

    Returns the file to send and, when it isn't in the requested format, a note saying
    what was sent instead and why.
    """
    ext = src.suffix.lower()
    if target == "original" or ext in NATIVE or ext not in CONVERTIBLE:
        return src, None
    fmt, reasons = CONVERTIBLE[ext], []

    def failed(step: str, e: Exception) -> None:
        reasons.append(reason(e))
        print(f"warning: {step} failed: {reasons[-1]}", file=sys.stderr)

    if target == "pdf" and fmt == "html":
        text = src.read_text(encoding="utf-8", errors="replace")
        if "<style" in text.lower():
            # Designed HTML: render it as-is with its own CSS. Converting it would lose that
            # styling, so if it can't be rendered, reflowable EPUB is the better fallback.
            try:
                html_pdf(ink_to_svg(text), src.parent, outdir / "document.pdf")
                return outdir / "document.pdf", None
            except RenderError as e:
                failed("rendering the HTML", e)
                target = "epub"
    try:
        ast = sanitize(read_doc(src, fmt, outdir), [src.parent, outdir / "media"], outdir)
    except Exception as e:
        failed("reading the document", e)
        ast = None
    if ast is not None and target == "pdf":
        out = outdir / "document.pdf"
        if CJK_TEXT.search(json.dumps(ast, ensure_ascii=False)) and not cjk_font_installed():
            reasons.append("no CJK font installed")
        else:
            try:
                typst_pdf(ast, outdir, title, out)
                return out, None
            except RenderError as e:
                failed("Typst PDF rendering", e)
            try:
                html_pdf(html_page(write_doc(ast, "html5"), title), outdir, out)
                return out, None
            except Exception as e:
                failed("HTML PDF rendering", e)
    if ast is not None:
        try:
            out = outdir / "document.epub"
            write_doc(ast, "epub3", f"--resource-path={outdir}", "--metadata", f"title={title}", outputfile=out)
            return out, (f"as EPUB (PDF unavailable: {'; '.join(reasons)})" if reasons else None)
        except Exception as e:
            failed("EPUB conversion", e)
    out = outdir / "document.txt"
    out.write_text(src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    return out, f"as plain text ({'; '.join(reasons)})"


def strip_ext(title: str) -> str:
    """Drop a known file extension only, so 'Notes v1.2' and 'Dr. Smith' stay whole."""
    p = Path(title)
    return safe_name(p.stem if p.suffix.lower() in NATIVE | set(CONVERTIBLE) else title)


def send_paths(paths: list[Path], target: str, title: str | None) -> None:
    b = client()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        for n, p in enumerate(paths):
            t = strip_ext(title) if title else p.stem
            work = Path(d) / str(n)  # short, fixed names: titles never become paths
            work.mkdir()
            out, note = convert(p, target, t, work)
            name = f"{t}{out.suffix}" if out is not p else (title or p.name)
            sent = b.send_file(out, name)
            print(f"Sent {note}: {sent}" if note else f"Sent: {sent}")


def check_files(paths: list[Path]) -> None:
    for p in paths:
        if not p.is_file():
            sys.exit(f"error: no such file: {p}")
        if p.stat().st_size == 0:
            sys.exit(f"error: empty file: {p}")


def cmd_send(a):
    paths = [Path(f).expanduser() for f in a.files]
    check_files(paths)
    if a.title and len(paths) > 1:
        sys.exit("error: --title only works when sending a single file")
    send_paths(paths, a.as_, a.title)


def read_stdin() -> str:
    """Piped text, decoded as UTF-8 whatever the console's code page (Windows pipes default to it)."""
    import codecs
    import locale

    data = sys.stdin.buffer.read()
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def cmd_send_text(a):
    if a.file:
        if a.text is not None:
            sys.exit("error: give the text or --file, not both")
        p = Path(a.file).expanduser()
        check_files([p])
        if CONVERTIBLE.get(p.suffix.lower()) not in ("markdown", "html"):
            sys.exit("error: --file takes Markdown, HTML or text (.md, .html, .txt)")
        send_paths([p], a.as_, a.title)
        return
    text = read_stdin() if a.text in (None, "-") else a.text
    if not text.strip():
        sys.exit("error: no text given")
    title = strip_ext(a.title or "Note")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        p = Path(d) / f"input.{a.format}"
        p.write_text(text, encoding="utf-8")
        send_paths([p], a.as_, title)


def cmd_list(a):
    # The website and tablet read push records from the sync gateway; the older
    # push/message API keeps listing files after they're deleted.
    from datetime import datetime

    docs = [d for d in changes(client(), "MESSAGE") if d.get("contentType") == "digital_content"]
    for d in sorted(docs, key=ts, reverse=True)[:a.limit]:
        print(f"{d['_id']}  {datetime.fromtimestamp(ts(d)):%Y-%m-%d %H:%M}  {d.get('name')}")


def changes(b: Boox, channel: str) -> list[dict]:
    """Live records in one of the account's sync gateway channels."""
    s, db = b.sync
    r = sg_request(s, "GET", f"{db}/_changes", params={
        "filter": "sync_gateway/bychannel", "channels": f"{b.uid}-{channel}", "include_docs": "true"})
    if r is None or not r.ok:
        sys.exit(f"error: couldn't read {channel}")
    return [x["doc"] for x in r.json().get("results", []) if x.get("doc") and not x["doc"].get("_deleted")]


# ---------------------------------------------------------------------------
# Handwriting. Notebooks (including PDFs annotated on current firmware) live in
# OSS under {uid}/note/{notebook}/, synced via the NOTE_TREE channel:
#   point/{page}#{rev}#points   pen samples (format below), one file per sync batch
#   shape/{page}#{rev}#{ms}.zip protobuf NoteShapeInfoProto list; the newest record
#                               for a stroke wins, shapeStatus=1 means erased
#   virtual/page/pb/*           page order and, for PDFs, the source page index
#   resource/content/*.pdf      the PDF a notebook annotates
# Formats are taken from the push.boox.com web app and the tablet's own uploads.
# ---------------------------------------------------------------------------

def parse_points(data: bytes) -> dict[str, list[tuple[float, float]]]:
    """Decode a points file: {stroke id: [(x, y), ...]} in page canvas units.

    Layout (big-endian): u16 magic, u16 version, 36-byte page id, 36-byte revision id;
    per stroke a 4-byte header then 16-byte samples (f32 x, f32 y, i16 size,
    i16 pressure, i32 time); an index of [36-byte stroke id][u32 offset][u32 length];
    and a final u32 giving the index offset.
    """
    import struct

    (index_at,) = struct.unpack(">I", data[-4:])
    strokes = {}
    for i in range(index_at, len(data) - 4 - 43, 44):
        sid = data[i:i + 36].rstrip(b"\0").decode()
        off, length = struct.unpack(">II", data[i + 36:i + 44])
        block = data[off + 4:off + length]
        strokes[sid] = [struct.unpack(">ff", block[j:j + 8]) for j in range(0, len(block) - 15, 16)]
    return strokes


def parse_strokes(data: bytes) -> list[list[tuple[float, float]]]:
    return list(parse_points(data).values())


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = sh = 0
    while True:
        c = b[i]; i += 1; r |= (c & 0x7F) << sh; sh += 7
        if c < 0x80:
            return r, i


def proto_fields(b: bytes) -> list[tuple[int, object]]:
    """Minimal protobuf decoder: [(field number, value)]."""
    import struct

    i, out = 0, []
    while i < len(b):
        key, i = _varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 1:
            v = b[i:i + 8]; i += 8
        elif wt == 5:
            (v,) = struct.unpack("<f", b[i:i + 4]); i += 4
        elif wt == 2:
            n, i = _varint(b, i); v = b[i:i + n]; i += n
        else:
            raise ValueError(f"unsupported wire type {wt}")
        out.append((f, v))
    return out


def _pb_varint(v: int) -> bytes:
    v &= (1 << 64) - 1
    out = bytearray()
    while True:
        c = v & 0x7F; v >>= 7
        out.append(c | 0x80 if v else c)
        if not v:
            return bytes(out)


def _pb(n: int, v) -> bytes:
    import struct

    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return _pb_varint(n << 3 | 2) + _pb_varint(len(v)) + v
    if isinstance(v, float):
        return _pb_varint(n << 3 | 5) + struct.pack("<f", v)
    return _pb_varint(n << 3) + _pb_varint(v)


class Notebook:
    def __init__(self, b: "Boox", record: dict):
        self.b, self.record = b, record
        self.id, self.title = record["_id"], record.get("title") or ""
        self.prefix = f"{b.uid}/note/{self.id}"

    @cached_property
    def objects(self) -> list:
        return sorted(self.b.storage.list(self.prefix + "/"), key=lambda o: o.last_modified)

    def read(self, key: str) -> bytes:
        return self.b.storage.get(key)

    def _shape_status(self, key: str, status: dict[str, int]) -> None:
        import zipfile

        z = zipfile.ZipFile(io.BytesIO(self.read(key)))
        info = z.infolist()[0]
        if info.file_size > 64 << 20:
            raise ValueError("shape file too large")
        for _, shape in proto_fields(z.read(info)):
            f = dict(proto_fields(shape))
            status[f[1].decode()] = f.get(15, 0)

    @cached_property
    def pages(self) -> list[dict]:
        """Pages in order: {id, pdf_index, width, height} (width/height of the stroke canvas)."""
        import re

        info = (self.record.get("notePageInfo") or {}).get("pageInfoMap") or {}
        order = ((self.record.get("pageNameList") or {}).get("pageNameList")) or list(info)
        pdf_index, canvas = {}, {}
        for o in self.objects:
            if "/virtual/page/" in o.key:
                d = self.read(o.key)
                for pid, rest in re.findall(rb"\$([0-9a-f-]{36})(.*?)(?=\$[0-9a-f-]{36}|$)", d, re.S):
                    pid = pid.decode()
                    if m := re.search(rb'"pageIndex":(\d+)', rest):
                        pdf_index[pid] = int(m.group(1))
                    if m := re.search(rb'"bottom":([\d.]+),"empty":false,"left":0\.0,"right":([\d.]+)', rest):
                        canvas[pid] = (float(m.group(2)), float(m.group(1)))
        ids = order or list(pdf_index)
        ids += [p for p in pdf_index if p not in ids]
        pages = []
        for pid in ids:
            w, h = canvas.get(pid) or (info.get(pid, {}).get("width", 2400), info.get(pid, {}).get("height", 3200))
            pages.append({"id": pid, "pdf_index": pdf_index.get(pid), "width": float(w), "height": float(h)})
        if all(p["pdf_index"] is not None for p in pages):
            pages.sort(key=lambda p: p["pdf_index"])
        return pages

    @cached_property
    def pdf_bytes(self) -> bytes | None:
        keys = [o.key for o in self.objects if "/resource/content/" in o.key and o.key.lower().endswith(".pdf")]
        return self.read(keys[0]) if keys else None

    def strokes(self, page_id: str) -> dict[str, list[tuple[float, float]]]:
        """Live (not erased) strokes on a page. Unreadable files are skipped with a warning."""
        status: dict[str, int] = {}
        points: dict[str, list] = {}
        for o in self.objects:  # oldest first, so the newest record wins
            try:
                if f"/shape/{page_id}#" in o.key:
                    self._shape_status(o.key, status)
                elif f"/point/{page_id}#" in o.key:
                    points.update(parse_points(self.read(o.key)))
            except Exception as e:
                print(f"warning: skipped unreadable {o.key.rsplit('/', 1)[-1]}: {e}", file=sys.stderr)
        return {sid: pts for sid, pts in points.items() if status.get(sid, 0) != 1}

    def render(self, out_dir: Path, scale: float = 2.0) -> list[Path]:
        import pypdfium2
        from PIL import Image, ImageDraw

        pdf = pypdfium2.PdfDocument(self.pdf_bytes) if self.pdf_bytes else None
        paths = []
        for n, page in enumerate(self.pages, 1):
            strokes = self.strokes(page["id"])
            if pdf is not None and page["pdf_index"] is not None and page["pdf_index"] < len(pdf):
                img = pdf[page["pdf_index"]].render(scale=scale).to_pil().convert("RGB")
            else:
                img = Image.new("RGB", (int(page["width"] / 2), int(page["height"] / 2)), "white")
            k = img.width / page["width"]
            draw = ImageDraw.Draw(img)
            for pts in strokes.values():
                xy = [(x * k, y * k) for x, y in pts]
                if len(xy) > 1:
                    draw.line(xy, fill=(220, 0, 0), width=max(2, round(img.width / 600)), joint="curve")
            path = out_dir / f"{safe_name(self.title)} p{n}.png"
            img.save(path)
            paths.append(path)
            print(f"{path}  ({len(strokes)} strokes)")
        return paths

    def write(self, page_no: int, strokes: list[list[tuple[float, float]]]) -> int:
        """Add pen strokes (canvas units) to a page as genuine, erasable ink."""
        import struct
        import time
        import zipfile

        page = self.pages[page_no - 1]
        pid, now = page["id"], int(time.time() * 1000)
        # Find the records to touch before uploading anything, so a failure leaves no orphans.
        docs = []
        for d in changes(self.b, "NOTE_TREE"):
            if d.get("_id") == self.id:
                d.setdefault("notePageInfo", {}).setdefault("pageInfoMap", {}).setdefault(pid, {})["lastModifyTime"] = now
                d["updatedAt"] = now
                docs.append(d)
            elif d.get("documentUniqueId") == self.id and "commitType" in d:
                d["updatedAt"] = now
                docs.append(d)
        if not any(d["_id"] == self.id for d in docs):
            sys.exit(f"error: notebook record {self.id} not found; nothing written")
        pts_rev, shape_rev = str(uuid.uuid4()), str(uuid.uuid4())
        ids = [str(uuid.uuid4()) for _ in strokes]

        body = bytearray(struct.pack(">HH", 0, 1) + pid.encode().ljust(36, b"\0") + pts_rev.encode().ljust(36, b"\0"))
        index, t = [], 0
        for sid, st in zip(ids, strokes):
            blk = bytearray(4)
            for x, y in st:
                blk += struct.pack(">ffhhi", x, y, 7925, 1, t); t += 8
            index.append((sid, len(body), len(blk)))
            body += blk
        index_at = len(body)
        for sid, off, ln in index:
            body += sid.encode().ljust(36, b"\0") + struct.pack(">II", off, ln)
        body += struct.pack(">I", index_at)

        create = ('{"alphaFactor":1.0,"displayScale":1.0,"dpi":320.0,"maxPressure":4095.0,"newBrushRatio":0.0,'
                  '"pressure":0.0,"pressureSensitivity":0.3,"smoothLevel":0.6,"source":0,"tiltX":0,"tiltY":0}')
        shapes = bytearray()
        for sid, st in zip(ids, strokes):
            xs, ys = [p[0] for p in st], [p[1] for p in st]
            rect = json.dumps({"bottom": max(ys), "empty": False, "left": min(xs), "right": max(xs),
                               "stability": 0, "top": min(ys)}, separators=(",", ":"))
            # NoteShapeInfoProto: 1 id, 2 created, 3 updated, 4 colour (ARGB black), 5 thickness,
            # 7 bounding rect, 11 pen args, 12 shape type (2 = pen), 16 points revision,
            # 17 line style, 18 shape revision, 21 links, 26 extra. Mirrors the tablet's strokes.
            msg = (_pb(1, sid) + _pb(2, now) + _pb(3, now) + _pb(4, -16777216) + _pb(5, 2.952755928039551)
                   + _pb(7, rect) + _pb(11, create) + _pb(12, 2) + _pb(16, pts_rev)
                   + _pb(17, '{"lineStyle":{"phase":0.0,"type":0}}') + _pb(18, shape_rev) + _pb(21, "[]")
                   + _pb(26, '{"repo":{}}'))
            shapes += _pb(1, msg)
        entry = f"{pid}#{shape_rev}#{now}"
        zbuf = io.BytesIO()
        with zipfile.ZipFile(zbuf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(entry, bytes(shapes))
        self.b.storage.put(f"{self.prefix}/point/{pid}#{pts_rev}#points", bytes(body))
        self.b.storage.put(f"{self.prefix}/shape/{entry}.zip", zbuf.getvalue())

        # Mark the page changed, as the tablet does after an edit, so it pulls the new ink.
        s, db = self.b.sync
        r = sg_request(s, "POST", f"{db}/_bulk_docs", json={"docs": docs})
        results = r.json() if r is not None and r.ok else []
        if r is None or not r.ok or any(x.get("error") and x.get("error") != "conflict" for x in results):
            sys.exit(f"error: strokes uploaded but the notebook record wasn't updated: {r.text[:200] if r is not None else 'timeout'}")
        if any(x.get("error") == "conflict" for x in results):
            # The tablet synced in between (or a timed-out attempt landed). The strokes are
            # stored; they show up the next time the notebook changes on either side.
            print("warning: the notebook changed during the write; the ink appears after its next sync", file=sys.stderr)
        return len(strokes)


def safe_name(name: str) -> str:
    """A file name that's valid on Windows, macOS and Linux."""
    name = re.sub(r'[\x00-\x1f\\/:*?"<>|]+', "_", name).strip()[:100].rstrip(". ")
    if re.fullmatch(r"(con|prn|aux|nul|com\d|lpt\d)(\..*)?", name, re.I):  # reserved on Windows
        name = "_" + name
    return name or "notebook"


def notebooks(b: "Boox") -> list[Notebook]:
    recs = [d for d in changes(b, "NOTE_TREE") if d.get("title") and "commitType" not in d]
    return [Notebook(b, d) for d in sorted(recs, key=ts, reverse=True)]


def find_notebook(b: "Boox", query: str) -> Notebook:
    """Match by ID, then exact title, then a unique title substring. Ambiguity is an error."""
    nbs = notebooks(b)
    q = query.strip().lower()
    matches = ([n for n in nbs if n.id == query.strip()]
               or [n for n in nbs if n.title.strip().lower() == q]
               or [n for n in nbs if q in n.title.lower()])
    if not matches:
        sys.exit(f"error: no notebook or annotated document matching '{query}' (run `ink` with no argument to list)")
    if len(matches) > 1:
        sys.exit(f"error: '{query}' matches {len(matches)} documents; use an ID:\n"
                 + "\n".join(f"  {n.id}  {n.title}" for n in matches))
    return matches[0]


def text_strokes(text: str, x: float, y: float, size: float, width: float) -> list[list[tuple[float, float]]]:
    """Single-line handwriting-style strokes for text, wrapped to `width`, top-left at (x, y)."""
    from HersheyFonts import HersheyFonts

    font = HersheyFonts()
    font.load_default_font("scripts")
    font.normalize_rendering(size)
    line_gap = size * 1.6
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        w = max((px for st in font.strokes_for_text(trial) for px, _ in st), default=0)
        if cur and w > width:
            lines.append(cur); cur = word
        else:
            cur = trial
    lines.append(cur)

    def densify(pts, step=3.0):
        out = [pts[0]]
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            n = max(1, int(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5 / step))
            out += [(ax + (bx - ax) * k / n, ay + (by - ay) * k / n) for k in range(1, n + 1)]
        return out

    strokes = []
    for row, line in enumerate(lines):
        base = y + size + row * line_gap
        for st in font.strokes_for_text(line):
            pts = [(x + px, base - py) for px, py in st]
            if len(pts) > 1:
                strokes.append(densify(pts))
    return strokes


def cmd_ink(a):
    """List notebooks / annotated documents, or render one's handwriting to PNGs."""
    b = client()
    if not a.document:
        for n in notebooks(b):
            print(f"{n.id}  {n.title}")
        return
    nb = find_notebook(b, a.document)
    out = Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    nb.render(out)


def cmd_write(a):
    """Write text onto a notebook page as real, erasable pen strokes."""
    text = (read_stdin() if a.text in (None, "-") else a.text).strip()
    if not text:
        sys.exit("error: no text given")
    if not (0 <= a.x < 1 and 0 <= a.y < 1 and 0 < a.size < 0.5 and (a.width is None or 0 < a.width <= 1)):
        sys.exit("error: --x and --y must be in [0, 1), --size in (0, 0.5), --width in (0, 1]")
    if unsupported := sorted({c for c in text if not (" " <= c <= "~") and not c.isspace()}):
        sys.exit(f"error: the handwriting font only covers ASCII; can't write {''.join(unsupported)!r}")
    b = client()
    nb = find_notebook(b, a.document)
    if not 1 <= a.page <= len(nb.pages):
        sys.exit(f"error: '{nb.title}' has {len(nb.pages)} page(s)")
    page = nb.pages[a.page - 1]
    # Positions are fractions of the page, so agents can aim using the rendered PNG.
    x, y = a.x * page["width"], a.y * page["height"]
    size = a.size * page["height"]
    width = (a.width or (0.95 - a.x)) * page["width"]
    n = nb.write(a.page, text_strokes(text, x, y, size, width))
    print(f"Wrote {n} strokes on '{nb.title}' ({nb.id}) page {a.page}. They appear on the tablet at its next note sync.")


SCREEN = (2400, 3200)  # default: Note Max panel, portrait (override with --screen)


def ink_to_svg(html: str) -> str:
    """Replace <ink>text</ink> with inline SVG handwriting (same script as `write`).

    Optional attributes: size (letter height in px of a 600px-wide layout, default 22),
    width (wrap width in px, default 470).
    """
    import re
    from html import unescape

    def render(m):
        attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        try:
            size, width = float(attrs.get("size", 22)), float(attrs.get("width", 470))
        except ValueError:
            size, width = 22.0, 470.0
        strokes = text_strokes(unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip(), 2, 2, size, width)
        if not strokes:
            return ""
        w = max(x for st in strokes for x, _ in st) + 4
        h = max(y for st in strokes for _, y in st) + size * 0.5
        paths = "".join('<polyline points="%s"/>' % " ".join(f"{x:.1f},{y:.1f}" for x, y in st) for st in strokes)
        return (f'<svg class="ink" xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
                f'viewBox="0 0 {w:.0f} {h:.0f}"><g fill="none" stroke="#000" stroke-width="{size / 14:.2f}" '
                f'stroke-linecap="round" stroke-linejoin="round">{paths}</g></svg>')

    return re.sub(r"<ink([^>]*)>(.*?)</ink>", render, html, flags=re.S)


def screen_image(src: Path, out: Path, screen: tuple[int, int] = SCREEN) -> Path:
    """Render an image, PDF or styled HTML page to a full-screen grayscale PNG."""
    from PIL import Image, ImageOps

    ext = src.suffix.lower()
    if ext in (".html", ".htm", ".pdf"):
        import pypdfium2

        if ext == ".pdf":
            pdf = pypdfium2.PdfDocument(src.read_bytes())
        else:
            rendered = out.with_name("page.pdf")
            try:
                html_pdf(ink_to_svg(src.read_text(encoding="utf-8")), src.parent, rendered)
            except RenderError as e:
                sys.exit(f"error: can't render the HTML: {e}. See `send2boox.py doctor`, or pass a PNG or PDF")
            pdf = pypdfium2.PdfDocument(rendered.read_bytes())
        page = pdf[0]
        img = page.render(scale=screen[0] / page.get_width()).to_pil()
    else:
        img = Image.open(src)
    img = ImageOps.exif_transpose(img).convert("L")
    img = ImageOps.pad(img, screen, color=255)  # letterbox in white, never crop
    img.save(out, optimize=True)
    return out


def push_screensaver(b: Boox, png: Path, name: str) -> None:
    """Upload a screensaver the way push.boox.com does and push it to the tablet."""
    import hashlib
    import time

    key = f"{b.uid}/push/{uuid.uuid4()}.png"
    b.storage.upload(key, png)
    data = png.read_bytes()
    now = int(time.time() * 1000)
    guid = str(uuid.uuid4())
    url = b.storage.sign_url(key, 10000)
    content = {"_id": guid, "createdAt": now, "distributeChannel": "onyx", "formats": ["png"], "guid": guid,
               "name": name, "ownerId": b.uid, "size": len(data), "md5": hashlib.md5(data).hexdigest(),
               "storage": {"png": {"oss": {"displayName": name, "expires": 0, "key": key, "provider": "oss",
                                           "size": len(data), "url": url}}},
               "title": name, "updatedAt": now}
    record = {"_id": guid, "contentType": "push_screensaver", "content": json.dumps(content), "msgType": 2,
              "dbId": f"{b.uid}-MESSAGE", "user": b.uid, "name": name, "size": len(data), "uniqueId": guid,
              "createdAt": now, "updatedAt": now}
    s, db = b.sync
    r = sg_request(s, "POST", f"{db}/_bulk_docs", json={"docs": [record]})
    result = (r.json() or [{}])[0] if r is not None and r.ok else {}
    if result.get("error") == "conflict":  # a timed-out attempt already created it
        g = sg_request(s, "GET", f"{db}/{guid}")
        result = {"rev": g.json()["_rev"]} if g is not None and g.ok else {}
    if not result.get("rev"):
        sys.exit(f"error: couldn't create the screensaver record: {r.text[:200] if r is not None else 'timeout'}")
    b.call("screenSavers/push", timeout=120, data={
        "data": {"name": name, "resourceDisplayName": name, "resourceKey": key, "bucket": b.storage.bucket,
                 "resourceType": "png", "title": name, "parent": None},
        "cbMsg": {"id": guid, "rev": result["rev"]}})


def cmd_screensaver(a):
    src = Path(a.file).expanduser()
    if not src.is_file():
        sys.exit(f"error: no such file: {src}")
    name = safe_name(strip_ext(a.title) if a.title else src.stem) + ".png"
    try:
        screen = tuple(int(v) for v in a.screen.lower().split("x"))
        assert len(screen) == 2 and 0 < min(screen) and max(screen) <= 10000
    except (ValueError, AssertionError):
        sys.exit("error: --screen must look like 2400x3200 (each side at most 10000)")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
        png = screen_image(src, Path(d) / "screen.png", screen)
        if a.preview:
            out = Path(a.preview).expanduser()
            out.write_bytes(png.read_bytes())
            print(f"Preview: {out}")
            return
        push_screensaver(client(), png, name)
    print(f"Pushed screensaver: {name}")


def cmd_delete(a):
    client().delete(a.ids)
    print(f"Deleted {len(a.ids)} file(s)")


def cmd_doctor(a):
    """Show which renderers this machine has, and what each kind of conversion will use."""
    import platform
    from importlib.metadata import version

    def check(fn):
        try:
            return True, fn()
        except Exception as e:
            return False, reason(e)

    def pandoc():
        import pypandoc

        return f"pandoc {pypandoc.get_pandoc_version()}"

    def typst():
        import typst  # noqa: F401

        return f"typst {version('typst')}"

    has_pandoc, pandoc_info = check(pandoc)
    has_typst, typst_info = check(typst)
    browser, wp = find_browser(), weasyprint()
    html_engine = browser or (None if isinstance(wp, str) else "WeasyPrint")
    docs = ("Typst" if has_typst else html_engine) if has_pandoc else None
    print(f"Python {platform.python_version()} on {platform.system()} {platform.machine()}")
    print(f"pandoc:     {pandoc_info}")
    print(f"Typst:      {typst_info}")
    print(f"browser:    {browser or 'none found (install Chrome, Edge or Chromium, or set BOOX_BROWSER)'}")
    print(f"WeasyPrint: {wp if isinstance(wp, str) else 'available'}")
    print(f"CJK fonts:  {'yes' if cjk_font_installed() else 'none, so CJK documents are sent as EPUB'}")
    print(f"login:      {'saved' if load_config().get('token') else 'not logged in'}")
    print(f"Markdown and documents -> PDF:        {docs or 'unavailable (sent as EPUB or text)'}")
    print(f"designed HTML and HTML sleep screens: {html_engine or 'unavailable (use a PNG or PDF)'}")


def main():
    for stream in (sys.stdout, sys.stderr):  # pipes on Windows default to the ANSI code page
        stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(required=True)
    s = sub.add_parser("login-request", help="email a 6-digit login code")
    s.add_argument("email")
    s.add_argument("--cloud", help=f"Send2Boox server (default {DEFAULT_CLOUD}; EU accounts: eur.boox.com)")
    s.set_defaults(fn=cmd_login_request)
    s = sub.add_parser("login", help="finish login with the emailed code")
    s.add_argument("code")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("status", help="check the saved login").set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="show which renderers this machine has").set_defaults(fn=cmd_doctor)
    s = sub.add_parser("send", help="send one or more files")
    s.add_argument("files", nargs="+")
    s.add_argument("--title", help="display name on the tablet (single file only)")
    s.add_argument("--as", dest="as_", default="original", choices=["original", "pdf", "epub"],
                   help="send unchanged (default), or convert Markdown/HTML/DOCX/etc. to PDF or EPUB")
    s.set_defaults(fn=cmd_send)
    s = sub.add_parser("send-text", help="send text (argument, stdin or --file) as a document")
    s.add_argument("text", nargs="?")
    s.add_argument("--file", help="read the text from a Markdown, HTML or text file (best on Windows)")
    s.add_argument("--title", help="document name, e.g. 'Meeting notes'")
    s.add_argument("--format", default="md", choices=["md", "html", "txt"], help="format of the input text (default md)")
    s.add_argument("--as", dest="as_", default="pdf", choices=["pdf", "epub", "original"],
                   help="format to send (default pdf; 'original' sends the raw text file)")
    s.set_defaults(fn=cmd_send_text)
    s = sub.add_parser("list", help="list recently sent files")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_list)
    s = sub.add_parser("ink", help="read handwriting: render a notebook or annotated PDF's pages with ink (PNG)")
    s.add_argument("document", nargs="?", help="notebook/document ID, exact title, or unique title substring; omit to list")
    s.add_argument("--out", default=".", help="directory for the PNGs (default: current directory)")
    s.set_defaults(fn=cmd_ink)
    s = sub.add_parser("write", help="write text onto a notebook page as real, erasable pen strokes")
    s.add_argument("document", help="notebook/document ID (preferred), exact title, or unique title substring")
    s.add_argument("text", nargs="?", help="text to write (or stdin)")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--x", type=float, default=0.08, help="left edge, as a fraction of page width (default 0.08)")
    s.add_argument("--y", type=float, default=0.1, help="top edge, as a fraction of page height (default 0.1)")
    s.add_argument("--size", type=float, default=0.022, help="letter height as a fraction of page height (default 0.022)")
    s.add_argument("--width", type=float, help="wrap width as a fraction of page width (default: to the right margin)")
    s.set_defaults(fn=cmd_write)
    s = sub.add_parser("screensaver", help="push an image, PDF or HTML page as the tablet's sleep screen")
    s.add_argument("file")
    s.add_argument("--title", help="name for the screensaver (default: file name)")
    s.add_argument("--preview", metavar="PNG", help="only render the screen image to this path; don't push")
    s.add_argument("--screen", default="2400x3200", help="screen size in pixels, portrait (default 2400x3200, Note Max)")
    s.set_defaults(fn=cmd_screensaver)
    s = sub.add_parser("delete", help="delete sent files by ID (see list); never notebooks")
    s.add_argument("ids", nargs="+")
    s.set_defaults(fn=cmd_delete)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
