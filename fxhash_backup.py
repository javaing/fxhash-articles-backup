#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fxhash-articles-backup
======================

Build a Cloudflare-Pages-ready static-site backup zip of an fxhash author's
articles, including:

- Article body rendered to HTML (markdown → html)
- Article cover thumbnails (downloaded from IPFS)
- Inline images / videos referenced in body (downloaded from IPFS)
- Tezos NFT embeds (`::tezos-storage-pointer[]{...}`) replaced with linked
  thumbnails — fxhash projects use the fxhash GraphQL API, other Tezos NFTs
  use the public TzKT API
- Static index.html, style.css, manifest.json, README.md

Usage:
    python fxhash_backup.py <username> [--output-dir DIR]

Output:
    <output-dir>/<username>-fxhash.zip   (default output dir: current dir)
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import markdown as md_lib

GRAPHQL = "https://api.fxhash.xyz/graphql"
TZKT = "https://api.tzkt.io/v1"
USER_AGENT = "fxhash-articles-backup/1.0 (+https://github.com/javaing/fxhash-articles-backup)"
PAGE_SIZE = 50  # fxhash GraphQL caps `take` at 50

IPFS_GATEWAYS = [
    "https://nftstorage.link/ipfs/{cid}",
    "https://cloudflare-ipfs.com/ipfs/{cid}",
    "https://ipfs.io/ipfs/{cid}",
    "https://{cid}.ipfs.dweb.link/",
    "https://{cid}.ipfs.cf-ipfs.com/",
]

FXHASH_ISSUER_V2 = "KT1BJC12dG17CVvPKJ1VYaNnaT5mzfnUTwXv"

TEZOS_PTR_RE = re.compile(r'::tezos-storage-pointer\[\]\{([^}]*)\}', re.MULTILINE)
VIDEO_DIR_RE = re.compile(r'::video\[\]\{([^}]*)\}', re.MULTILINE)
EMBED_MEDIA_RE = re.compile(r'::embed-media\[\]\{([^}]*)\}', re.MULTILINE)
GENERIC_DIR_RE = re.compile(r'::([a-zA-Z][\w-]*)\[\]\{([^}]*)\}', re.MULTILINE)
IPFS_REF_RE = re.compile(r'ipfs://([A-Za-z0-9]+(?:/[^\s)\]>]+)?)')


# ---------- HTTP / GraphQL ----------
def http_post_json(url: str, payload: dict, timeout: int = 30) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def http_get_json(url: str, timeout: int = 20) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def gql(query: str, variables: dict | None = None) -> dict:
    return http_post_json(GRAPHQL, {"query": query, "variables": variables or {}})


def gql_user_articles(username: str) -> list[dict]:
    """Fetch all article stubs for a user, paginated."""
    out: list[dict] = []
    skip = 0
    while True:
        q = (
            'query($n:String,$skip:Int,$take:Int){'
            'user(name:$n){id name '
            'articles(take:$take,skip:$skip){id slug}'
            '}}'
        )
        data = gql(q, {"n": username, "skip": skip, "take": PAGE_SIZE})
        user = (data.get("data") or {}).get("user")
        if not user:
            return []
        page = user.get("articles") or []
        out.extend(page)
        if len(page) < PAGE_SIZE:
            return out
        skip += PAGE_SIZE


def gql_article(slug: str) -> dict | None:
    q = ('query($slug:String){article(slug:$slug){'
         'id slug title body description language tags createdAt '
         'thumbnailUri artifactUri displayUri metadataUri editions royalties '
         'mintOpHash author{id name}}}')
    return (gql(q, {"slug": slug}).get("data") or {}).get("article")


def gql_generative_token(token_id: str) -> dict | None:
    q = ('{generativeToken(id:%s){id slug name '
         'thumbnailUri displayUri author{name}}}' % token_id)
    return (gql(q).get("data") or {}).get("generativeToken")


def fetch_ipfs(cid_with_path: str) -> tuple[bytes, str]:
    parts = cid_with_path.split("/", 1)
    cid = parts[0]
    path = parts[1] if len(parts) > 1 else ""
    last_err: Exception | None = None
    for tmpl in IPFS_GATEWAYS:
        url = tmpl.format(cid=cid)
        if path:
            url = url.rstrip("/") + "/" + path
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read(), r.headers.get("Content-Type", "")
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"all gateways failed for {cid_with_path}: {last_err}")


def ext_from_ct(ct: str, default: str = ".bin") -> str:
    if not ct:
        return default
    m = ct.split(";")[0].strip()
    e = mimetypes.guess_extension(m) or default
    return ".jpg" if e == ".jpe" else e


# ---------- helpers ----------
# ---------- i18n ----------
STRINGS: dict[str, dict[str, str]] = {
    "zh-Hant": {
        "html_lang": "zh-Hant",
        "site_title": "{username} 的 fxhash 文集",
        "post_title_suffix": " — {username} 的 fxhash 文集",
        "back": "← 回到文集",
        "by_author": "作者",
        "view_original": "在 fxhash 看原文",
        "editions_label": "版次",
        "ipfs": "IPFS",
        "footer_post": "由 fxhash GraphQL API 備份 · 原文 © {author}",
        "footer_index": "備份自 fxhash · 原文 © {username}",
        "hero_summary": ('共 {count} 篇。原發表於 <a href="{profile}">fxhash @{username}</a>，'
                         '由 <a href="{repo}">fxhash-articles-backup</a> 備份。'),
        "embed_open_on": "在 {label} 開啟",
        "embed_by": "by",  # keep English; works in CJK context too
    },
    "en": {
        "html_lang": "en",
        "site_title": "{username}'s fxhash articles",
        "post_title_suffix": " — {username}'s fxhash articles",
        "back": "← Back to articles",
        "by_author": "by",
        "view_original": "View on fxhash",
        "editions_label": "editions",
        "ipfs": "IPFS",
        "footer_post": "Backed up via fxhash GraphQL · © {author}",
        "footer_index": "Backed up from fxhash · © {username}",
        "hero_summary": ('{count} articles, originally published at '
                         '<a href="{profile}">fxhash @{username}</a>. '
                         'Backed up by <a href="{repo}">fxhash-articles-backup</a>.'),
        "embed_open_on": "open on {label}",
        "embed_by": "by",
    },
}

CJK_RE = re.compile(r'[一-鿿㐀-䶿]')


def detect_lang(articles: list[dict]) -> str:
    """Heuristic: if articles' bodies have substantial CJK content, return 'zh-Hant'."""
    cjk_count = 0
    ascii_letter_count = 0
    for a in articles:
        body = a.get("body") or ""
        cjk_count += len(CJK_RE.findall(body))
        ascii_letter_count += sum(1 for c in body if c.isascii() and c.isalpha())
    return "zh-Hant" if cjk_count >= 200 or cjk_count > ascii_letter_count // 6 else "en"


def slugify_filename(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9._-]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-.")
    return s or "untitled"


def safe_print(msg: str) -> None:
    """Avoid Windows codepage crashes when stdout can't encode unicode."""
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.write(msg.encode(enc, errors="replace").decode(enc, errors="replace") + "\n")
        sys.stdout.flush()


# ---------- Tezos NFT embed ----------
def parse_tezos_pointer(m: re.Match[str]) -> tuple[str, str, str] | None:
    attrs = m.group(1)
    cm = re.search(r'contract="([^"]+)"', attrs)
    pm = re.search(r'path="([^"]+)"', attrs)
    if not (cm and pm):
        return None
    contract = cm.group(1)
    path = pm.group(1)
    idm = re.match(r'(?:ledger|token_metadata)::(\d+)', path)
    if not idm:
        return None
    return contract, idm.group(1), path


def render_embed_media(m: re.Match[str]) -> str:
    """Render ::embed-media[]{href="..."} as a labelled card linking to the source."""
    attrs = m.group(1)
    href_m = re.search(r'href="([^"]+)"', attrs)
    if not href_m:
        return f'<div class="embed-media"><p>🔗 {html.escape(attrs)}</p></div>'
    href = href_m.group(1).rstrip('`:')  # strip authoring-artifact trailers
    href_safe = html.escape(href, quote=True)
    try:
        from urllib.parse import urlparse
        parsed = urlparse(href)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
    except Exception:
        host, path = "", ""

    icon, label = "🔗", host or "link"
    if host in ("twitter.com", "mobile.twitter.com", "x.com"):
        icon = "𝕏"
        um = re.match(r"/([A-Za-z0-9_]+)/status/", path)
        label = f"Tweet by @{um.group(1)}" if um else "Tweet"
    elif host == "youtu.be" or host.endswith("youtube.com"):
        icon = "▶"
        label = "YouTube video"
    elif host == "open.spotify.com":
        icon = "🎧"
        if path.startswith("/episode/"):
            label = "Spotify episode"
        elif path.startswith("/track/"):
            label = "Spotify track"
        elif path.startswith("/show/"):
            label = "Spotify show"
        elif path.startswith("/playlist/"):
            label = "Spotify playlist"
        else:
            label = "Spotify"
    elif host.endswith("instagram.com"):
        icon, label = "📷", "Instagram post"
    elif host.endswith("tiktok.com"):
        icon, label = "🎵", "TikTok video"
    elif host.endswith("vimeo.com"):
        icon, label = "▶", "Vimeo video"

    return (
        f'<p class="embed-media"><a href="{href_safe}" target="_blank" rel="noopener">'
        f'<span class="embed-icon">{icon}</span> '
        f'<span class="embed-label">{html.escape(label)}</span> '
        f'<span class="embed-url">{html.escape(href)}</span>'
        f'</a></p>'
    )


def render_unknown_directive(m: re.Match[str]) -> str:
    """Fallback for unrecognized ::name[]{...} directives so they don't leak as raw text."""
    name = m.group(1)
    attrs = m.group(2)
    href_m = re.search(r'href="([^"]+)"', attrs)
    src_m = re.search(r'src="([^"]+)"', attrs)
    url = (href_m or src_m).group(1) if (href_m or src_m) else ""
    if url:
        url_safe = html.escape(url, quote=True)
        return (f'<p class="embed-media"><a href="{url_safe}" target="_blank" '
                f'rel="noopener">🔗 {html.escape(name)} · {html.escape(url)}</a></p>')
    return f'<p class="embed-media">🔗 {html.escape(name)}</p>'


def render_video_directive(m: re.Match[str]) -> str:
    attrs = m.group(1)
    src_m = re.search(r'src="([^"]+)"', attrs)
    if not src_m:
        return m.group(0)
    src = src_m.group(1)
    return (f'<video src="{html.escape(src, quote=True)}" '
            f'controls playsinline preload="metadata"></video>')


def make_render_tezos_pointer(nft_info: dict[tuple[str, str], dict],
                               s: dict[str, str]):
    def render(m: re.Match[str]) -> str:
        parsed = parse_tezos_pointer(m)
        if not parsed:
            attrs = m.group(1)
            return (f'<div class="embed-tezos"><p>📦 Tezos NFT — '
                    f'<code>{html.escape(attrs)}</code></p></div>')
        contract, token_id, _ = parsed
        info = nft_info.get((contract, token_id), {})
        link_url = info.get("link") or f"https://objkt.com/tokens/{contract}/{token_id}"
        label = "fxhash" if "fxhash.xyz" in link_url else "objkt.com"
        name = info.get("name") or f"#{token_id}"
        creator = info.get("creator") or ""
        thumb = info.get("thumb")
        creator_html = (f' <span class="meta">{s["embed_by"]} {html.escape(creator)}</span>'
                        if creator else "")
        if thumb:
            return (
                f'<figure class="embed-nft">'
                f'<a href="{link_url}" target="_blank" rel="noopener">'
                f'<img src="../{thumb}" alt="{html.escape(name)}" loading="lazy" />'
                f'</a>'
                f'<figcaption><a href="{link_url}" target="_blank" rel="noopener">'
                f'{html.escape(name)}</a>{creator_html} '
                f'<span class="meta">· {label}</span></figcaption>'
                f'</figure>'
            )
        open_on = s["embed_open_on"].format(label=label)
        return (
            f'<div class="embed-tezos"><p>📦 <a href="{link_url}" '
            f'target="_blank" rel="noopener">{html.escape(name)} ({open_on})</a>'
            f'{creator_html}</p></div>'
        )
    return render


# ---------- markdown → HTML ----------
def md_to_html(body_md: str, render_tezos_pointer) -> str:
    placeholders: dict[str, str] = {}

    def stash_tez(m: re.Match[str]) -> str:
        key = f"@@TEZ_PTR_{abs(hash(m.group(0)))}@@"
        placeholders[key] = render_tezos_pointer(m)
        return f"\n\n{key}\n\n"

    def stash_video(m: re.Match[str]) -> str:
        key = f"@@VIDEO_{abs(hash(m.group(0)))}@@"
        placeholders[key] = render_video_directive(m)
        return f"\n\n{key}\n\n"

    def stash_embed(m: re.Match[str]) -> str:
        key = f"@@EMBED_{abs(hash(m.group(0)))}@@"
        placeholders[key] = render_embed_media(m)
        return f"\n\n{key}\n\n"

    def stash_generic(m: re.Match[str]) -> str:
        # Skip already-handled directive names; this catches the rest.
        if m.group(1) in ("tezos-storage-pointer", "video", "embed-media"):
            return m.group(0)
        key = f"@@DIR_{abs(hash(m.group(0)))}@@"
        placeholders[key] = render_unknown_directive(m)
        return f"\n\n{key}\n\n"

    body = TEZOS_PTR_RE.sub(stash_tez, body_md)
    body = VIDEO_DIR_RE.sub(stash_video, body)
    body = EMBED_MEDIA_RE.sub(stash_embed, body)
    body = GENERIC_DIR_RE.sub(stash_generic, body)
    out = md_lib.markdown(body, extensions=["extra", "sane_lists", "nl2br"])
    for k, v in placeholders.items():
        out = out.replace(f"<p>{k}</p>", v).replace(k, v)
    # Convert any `<img src=…(mp4|webm|mov|m4v)>` to <video>
    out = re.sub(
        r'<img[^>]+src="([^"]+\.(?:mp4|webm|mov|m4v))"[^>]*/?>',
        lambda m: (f'<video src="{m.group(1)}" controls '
                   f'playsinline preload="metadata"></video>'),
        out, flags=re.IGNORECASE,
    )
    return out


# ---------- main pipeline ----------
def build(username: str, output_dir: Path, lang: str = "auto") -> Path:
    work = Path(tempfile.mkdtemp(prefix="fxh_build_"))
    site = work / "site"
    (site / "posts").mkdir(parents=True)
    (site / "assets").mkdir(parents=True)

    safe_print(f"[1/4] Fetching article list for {username}…")
    stubs = gql_user_articles(username)
    if not stubs:
        sys.stderr.write(f"No articles found for user '{username}'.\n")
        sys.exit(2)
    safe_print(f"      found {len(stubs)} articles")

    safe_print("[2/4] Fetching article bodies…")
    articles: list[dict] = []
    for stub in stubs:
        a = gql_article(stub["slug"])
        if not a:
            safe_print(f"      ! skip {stub['slug']} (article not returned)")
            continue
        articles.append(a)
        safe_print(f"      • {a['slug']}  ({len(a['body'] or '')} chars body)")
    articles.sort(key=lambda a: a["createdAt"], reverse=True)
    if not articles:
        sys.stderr.write("All articles failed to fetch.\n")
        sys.exit(2)

    # Resolve language
    if lang == "auto":
        lang = detect_lang(articles)
    if lang not in STRINGS:
        sys.stderr.write(f"Unknown lang '{lang}', falling back to 'en'.\n")
        lang = "en"
    s = STRINGS[lang]
    safe_print(f"      site language: {lang}")

    asset_map: dict[str, str] = {}    # ipfs_uri -> "assets/xxx.ext"
    asset_bytes_total = 0

    def fetch_to_assets(uri: str | None, name_prefix: str = "img") -> tuple[str, int] | None:
        nonlocal asset_bytes_total
        if not uri or not uri.startswith("ipfs://"):
            return None
        if uri in asset_map:
            return asset_map[uri], 0
        cid_path = uri[len("ipfs://"):]
        try:
            data, ct = fetch_ipfs(cid_path)
            digest = hashlib.sha1(uri.encode()).hexdigest()[:10]
            ext = ext_from_ct(ct, ".jpg")
            fname = f"{name_prefix}-{digest}{ext}"
            (site / "assets" / fname).write_bytes(data)
            asset_map[uri] = f"assets/{fname}"
            asset_bytes_total += len(data)
            safe_print(f"      {name_prefix} {uri[:36]}… → {fname} ({len(data)} bytes)")
            return asset_map[uri], len(data)
        except Exception as e:
            safe_print(f"      ! {name_prefix} {uri} FAILED: {e}")
            return None

    safe_print("[3/4] Downloading IPFS assets (thumbnails + inline + NFT embeds)…")
    # Cover thumbnails
    for a in articles:
        fetch_to_assets(a.get("thumbnailUri"), name_prefix="thumb")
    # Inline IPFS refs in markdown body
    for a in articles:
        for m in IPFS_REF_RE.finditer(a.get("body") or ""):
            fetch_to_assets("ipfs://" + m.group(1), name_prefix="img")

    # Tezos-storage-pointer NFT embeds
    nft_info: dict[tuple[str, str], dict] = {}
    pointers: set[tuple[str, str]] = set()
    for a in articles:
        for m in TEZOS_PTR_RE.finditer(a.get("body") or ""):
            p = parse_tezos_pointer(m)
            if p:
                pointers.add((p[0], p[1]))
    safe_print(f"      [tezos NFTs] resolving {len(pointers)} unique embeds…")

    for contract, token_id in pointers:
        try:
            name, creator, thumb_uri, link_url = None, None, None, None
            if contract == FXHASH_ISSUER_V2:
                gt = gql_generative_token(token_id)
                if not gt:
                    safe_print(f"      ! fxhash gen #{token_id}: not found")
                    continue
                name = gt.get("name") or f"#{token_id}"
                creator = ((gt.get("author") or {}).get("name") or "").strip() or ""
                thumb_uri = gt.get("thumbnailUri") or gt.get("displayUri")
                slug = gt.get("slug")
                link_url = (f"https://www.fxhash.xyz/generative/slug/{slug}"
                            if slug else f"https://www.fxhash.xyz/generative/{token_id}")
            else:
                rows = http_get_json(
                    f"{TZKT}/tokens?contract={contract}&tokenId={token_id}"
                    f"&select=metadata,firstMinter"
                )
                if not rows:
                    safe_print(f"      ! NFT {contract}#{token_id}: not on TzKT")
                    continue
                md = rows[0].get("metadata") or {}
                first_minter = (rows[0].get("firstMinter") or {}).get("alias") or ""
                name = md.get("name") or f"#{token_id}"
                creators = md.get("creators") or []
                creator = creators[0] if creators else first_minter
                thumb_uri = (md.get("thumbnailUri") or md.get("displayUri")
                             or md.get("artifactUri"))
                link_url = f"https://objkt.com/tokens/{contract}/{token_id}"

            local = None
            if thumb_uri and thumb_uri.startswith("ipfs://"):
                r2 = fetch_to_assets(thumb_uri, name_prefix=f"nft-{token_id}")
                if r2 is not None:
                    local = asset_map.get(thumb_uri)
            nft_info[(contract, token_id)] = {
                "name": name, "creator": creator,
                "thumb": local, "link": link_url,
            }
            safe_print(f"      NFT {contract[:14]}…#{token_id} → {name[:40]} "
                       f"({'thumb ok' if local else 'no thumb'})")
        except Exception as e:
            safe_print(f"      ! NFT {contract}#{token_id} FAILED: {e}")

    safe_print("[4/4] Rendering HTML & writing manifest…")
    render_tezos_pointer = make_render_tezos_pointer(nft_info, s)

    def rewrite_ipfs_in_md(body_md: str) -> str:
        def repl(m: re.Match[str]) -> str:
            uri = "ipfs://" + m.group(1)
            local = asset_map.get(uri)
            if local:
                return "../" + local
            cid_path = m.group(1)
            cid = cid_path.split("/", 1)[0]
            rest = cid_path[len(cid):]
            return f"https://cloudflare-ipfs.com/ipfs/{cid}{rest}"
        return IPFS_REF_RE.sub(repl, body_md)

    manifest_articles: list[dict] = []
    posts_meta: list[tuple[dict, str, str | None]] = []

    for a in articles:
        created = a["createdAt"][:10]
        slug_safe = slugify_filename(a["slug"])
        post_filename = f"{created}-{slug_safe}.html"
        body_html = md_to_html(rewrite_ipfs_in_md(a.get("body") or ""), render_tezos_pointer)
        thumb_local = asset_map.get(a.get("thumbnailUri") or "")
        thumb_html = ""
        if thumb_local:
            thumb_html = f'<figure class="thumb"><img src="../{thumb_local}" alt="" /></figure>\n'
        cid = (a.get("thumbnailUri") or "").replace("ipfs://", "")
        gateways_html = ""
        if cid:
            base_cid = cid.split("/")[0]
            gateways_html = (f' · <a href="https://cloudflare-ipfs.com/ipfs/{base_cid}/" '
                             f'target="_blank" rel="noopener">IPFS</a>')
        tags_html = ""
        if a.get("tags"):
            tags_html = '<p class="tags">' + "".join(
                f'<span class="tag">{html.escape(t)}</span>' for t in a["tags"]
            ) + "</p>\n"
        title_esc = html.escape(a["title"])
        desc_esc = html.escape(a.get("description") or "")
        editions = a.get("editions", 0)
        author = (a.get("author") or {}).get("name") or username

        post_title_suffix = s["post_title_suffix"].format(username=html.escape(username))
        editions_str = (f"{s['editions_label']} {editions}" if lang == "zh-Hant"
                        else f"{editions} {s['editions_label']}")
        page = f"""<!doctype html>
<html lang="{s['html_lang']}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title_esc}{post_title_suffix}</title>
  <meta name="description" content="{desc_esc}" />
  <link rel="stylesheet" href="../style.css" />
</head>
<body>
  <main><article>
      <p><a href="../index.html">{s['back']}</a></p>
      <h1>{title_esc}</h1>
      <p class="meta"><time datetime="{a['createdAt']}">{created}</time>
        · {s['by_author']} <a href="https://www.fxhash.xyz/u/{html.escape(author)}">@{html.escape(author)}</a>
        · <a href="https://old.fxhash.xyz/article/{html.escape(a['slug'])}" target="_blank" rel="noopener">{s['view_original']}</a>{gateways_html}
        · {editions_str}</p>
      {tags_html}{thumb_html}<div class="content">
{body_html}
      </div>
    </article></main>
  <footer>{s['footer_post'].format(author=html.escape(author))}</footer>
</body>
</html>
"""
        (site / "posts" / post_filename).write_text(page, encoding="utf-8")

        manifest_articles.append({
            "id": a["id"],
            "slug": a["slug"],
            "title": a["title"],
            "description": a.get("description") or "",
            "language": a.get("language") or "",
            "tags": a.get("tags") or [],
            "createdAt": a["createdAt"],
            "editions": a.get("editions", 0),
            "royalties": a.get("royalties", 0),
            "thumbnailUri": a.get("thumbnailUri") or "",
            "artifactUri": a.get("artifactUri") or "",
            "displayUri": a.get("displayUri") or "",
            "metadataUri": a.get("metadataUri") or "",
            "mintOpHash": a.get("mintOpHash") or "",
            "author": author,
            "file": f"posts/{post_filename}",
            "sourceUrl": f"https://old.fxhash.xyz/article/{a['slug']}",
            "thumbnailLocal": thumb_local or "",
        })
        posts_meta.append((a, post_filename, thumb_local))

    # ---- index.html ----
    items_html = []
    for a, post_filename, thumb_local in posts_meta:
        thumb_img = (f'<img class="post-thumb" src="{thumb_local}" alt="" />'
                     if thumb_local else '')
        desc = (a.get("description") or "")[:140]
        items_html.append(f"""<li>
        {thumb_img}
        <div class="post-info">
          <a href="posts/{post_filename}">{html.escape(a['title'])}</a>
          <p class="post-desc">{html.escape(desc)}</p>
        </div>
        <time datetime="{a['createdAt']}">{a['createdAt'][:10]}</time>
      </li>""")
    site_title = s["site_title"].format(username=html.escape(username))
    profile_url = f"https://www.fxhash.xyz/u/{html.escape(username)}/articles"
    repo_url = "https://github.com/javaing/fxhash-articles-backup"
    hero_summary = s["hero_summary"].format(
        count=len(articles), profile=profile_url,
        username=html.escape(username), repo=repo_url,
    )
    index_html = f"""<!doctype html>
<html lang="{s['html_lang']}">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{site_title}</title>
  <link rel="stylesheet" href="style.css" />
</head>
<body>
  <main>
    <section class="hero">
      <h1>{site_title}</h1>
      <p>{hero_summary}</p>
    </section>
    <ol class="post-list">
{chr(10).join(items_html)}
    </ol>
  </main>
  <footer>{s['footer_index'].format(username=html.escape(username))}</footer>
</body>
</html>
"""
    (site / "index.html").write_text(index_html, encoding="utf-8")

    # ---- style.css ----
    (site / "style.css").write_text(STYLE_CSS, encoding="utf-8")

    # ---- manifest.json ----
    manifest = {
        "schema": "fxhash-articles-backup/v1",
        "exportedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "source": {
            "platform": "fxhash",
            "endpoint": GRAPHQL,
            "userName": username,
            "profileUrl": f"https://old.fxhash.xyz/u/{username}/articles",
        },
        "stats": {
            "totalArticles": len(articles),
            "totalAssets": len(asset_map),
            "assetBytes": asset_bytes_total,
            "totalNftEmbeds": len(pointers),
        },
        "articles": manifest_articles,
        "notice": (
            "本備份僅供作者本人或讀者個人 archival 用途。原作者保留所有權利；"
            "fxhash 文章常含 Tezos NFT 嵌入指引（::tezos-storage-pointer），"
            "本備份將其轉為 fxhash / objkt.com 連結並下載一張代表縮圖，"
            "但不收錄 NFT 媒體本身。"
        ),
    }
    (site / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # ---- AGENT.md, README.md ----
    (site / "AGENT.md").write_text(AGENT_TMPL[lang].format(username=username), encoding="utf-8")
    (site / "README.md").write_text(
        SITE_README_TMPL[lang].format(
            username=username, count=len(articles),
            graphql=GRAPHQL,
            profile=f"https://old.fxhash.xyz/u/{username}/articles",
        ),
        encoding="utf-8",
    )

    # ---- zip ----
    output_dir.mkdir(parents=True, exist_ok=True)
    out_zip = output_dir / f"{username}-fxhash.zip"
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in site.rglob("*"):
            if p.is_file():
                zf.write(p, p.relative_to(site))

    # cleanup work tree (keep only zip)
    shutil.rmtree(work, ignore_errors=True)

    safe_print("")
    safe_print(f"Done: {out_zip} ({out_zip.stat().st_size:,} bytes)")
    safe_print(f"  articles: {len(articles)}, "
               f"assets: {len(asset_map)} ({asset_bytes_total:,} bytes), "
               f"NFT embeds: {len(pointers)}")
    return out_zip


# ---------- templates ----------
STYLE_CSS = """:root {
  color-scheme: light;
  --ink: #151515;
  --muted: #626262;
  --line: #e5e1da;
  --paper: #fffdfa;
  --wash: #f6f4ef;
  --brand: #245f53;
  --accent: #c44536;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--wash);
  color: var(--ink);
  font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Noto Sans TC", "PingFang TC", sans-serif;
  line-height: 1.78;
}
main {
  width: min(860px, calc(100% - 32px));
  margin: 0 auto;
  padding: 44px 0 56px;
}
a { color: var(--brand); text-underline-offset: 3px; }
.hero { border-bottom: 1px solid var(--line); padding-bottom: 24px; margin-bottom: 12px; }
h1 { font-size: clamp(30px, 5vw, 54px); line-height: 1.12; margin: 0 0 12px; }
article h1 { font-size: clamp(28px, 4vw, 42px); }
h2, h3 { margin-top: 1.6em; line-height: 1.25; }
.meta, time, footer { color: var(--muted); font-size: 14px; }
.post-list { list-style: none; padding: 0; margin: 0; }
.post-list li {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr) auto;
  gap: 16px;
  padding: 18px 0;
  border-bottom: 1px solid var(--line);
  align-items: center;
}
.post-thumb { width: 96px; height: 96px; object-fit: cover; border: 1px solid var(--line); }
.post-info a { font-size: 18px; font-weight: 650; display: block; margin-bottom: 4px; }
.post-desc { color: var(--muted); margin: 0; font-size: 14px; }
.tag {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 2px 9px;
  margin: 2px 6px 2px 0;
  color: var(--muted);
  font-size: 13px;
}
.content {
  background: var(--paper);
  border: 1px solid var(--line);
  padding: clamp(18px, 4vw, 40px);
  margin-top: 24px;
}
.thumb { margin: 24px 0; }
.thumb img { max-width: 100%; height: auto; border: 1px solid var(--line); }
img, video { max-width: 100%; height: auto; }
video { background: #000; display: block; margin: 18px auto; }
figure { margin: 24px 0; }
blockquote { border-left: 4px solid var(--accent); margin-left: 0; padding-left: 16px; color: var(--muted); }
hr { border: 0; border-top: 1px solid var(--line); margin: 28px 0; }
code { background: var(--wash); padding: 1px 6px; border-radius: 3px; font-size: 0.92em; }
.embed-tezos {
  background: var(--wash);
  border-left: 4px solid var(--accent);
  padding: 12px 16px;
  margin: 18px 0;
  font-size: 14px;
}
.embed-tezos p { margin: 4px 0; }
.embed-tezos .meta { word-break: break-all; }
.embed-nft {
  margin: 24px 0;
  padding: 12px;
  background: var(--wash);
  border: 1px solid var(--line);
  text-align: center;
}
.embed-nft img {
  max-width: 100%;
  max-height: 480px;
  height: auto;
  border: 1px solid var(--line);
  background: #fff;
}
.embed-nft figcaption {
  margin-top: 8px;
  font-size: 14px;
  color: var(--muted);
}
.embed-nft figcaption a { color: var(--ink); font-weight: 600; }
.embed-media {
  margin: 14px 0;
  padding: 0;
}
.embed-media a {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 14px;
  background: var(--wash);
  border: 1px solid var(--line);
  border-left: 4px solid var(--accent);
  border-radius: 3px;
  color: var(--ink);
  text-decoration: none;
  font-size: 14px;
}
.embed-media a:hover { background: var(--paper); }
.embed-media .embed-icon { font-size: 16px; line-height: 1; }
.embed-media .embed-label { font-weight: 600; }
.embed-media .embed-url {
  color: var(--muted);
  font-size: 12px;
  word-break: break-all;
  flex: 1 1 auto;
}
footer {
  width: min(860px, calc(100% - 32px));
  margin: 0 auto;
  border-top: 1px solid var(--line);
  padding: 20px 0 36px;
}
@media (max-width: 640px) {
  .post-list li { grid-template-columns: 64px 1fr; }
  .post-list time { grid-column: 1 / -1; }
  .post-thumb { width: 64px; height: 64px; }
}
"""

AGENT_TMPL = {
    "zh-Hant": """# AGENT.md

這是 {username} 的 fxhash 文章靜態網站包。

部署規則：
- 直接把整個資料夾上傳到 Cloudflare Workers & Pages 的 Upload your static files。
- 不需要 npm install。
- 不需要 build command。
- 若使用 GitHub Pages、Netlify、Vercel static upload，也請把根目錄當 publish directory。
""",
    "en": """# AGENT.md

This is a static-site bundle of @{username}'s fxhash articles.

Deploy rules:
- Drag the whole folder into Cloudflare Workers & Pages → Upload your static files.
- No `npm install` required.
- No build command required.
- For GitHub Pages, Netlify, or Vercel static upload, treat the root folder as the publish directory.
""",
}

SITE_README_TMPL = {
    "zh-Hant": """# {username}-fxhash

@{username} 的 fxhash 靜態文章備份站，共 {count} 篇。

## 最簡單部署

1. 解壓縮這個 zip。
2. 打開 Cloudflare Workers & Pages，選 Upload your static files。
3. 把解壓後的整個資料夾拖上去。
4. 看到檔案清單後，按 Deploy。

不需要安裝 Node.js，不需要 GitHub，不需要 build command。

## 內容

- `index.html` — 文集首頁（依日期由新到舊）
- `posts/` — 每篇一個 HTML 檔
- `assets/` — 文章封面縮圖、內文圖片、影片、嵌入 NFT 縮圖（從 IPFS 下載）
- `manifest.json` — 機器可讀的備份索引
- `style.css` — 樣式表

## 來源與授權

- 來源：fxhash GraphQL API（`{graphql}`）
- 原始貼文位置：<{profile}>
- 文章中嵌入的 Tezos NFT（`tezos-storage-pointer`）已轉為 fxhash / objkt.com 連結，並下載一張代表縮圖；NFT 本體媒體未收錄。

## 重新產生

由 [fxhash-articles-backup](https://github.com/javaing/fxhash-articles-backup) 工具產出。
""",
    "en": """# {username}-fxhash

A static-site backup of @{username}'s fxhash articles — {count} posts.

## Easiest deploy

1. Unzip this archive.
2. Open Cloudflare Workers & Pages → Upload your static files.
3. Drag the unzipped folder in.
4. After the file list appears, click Deploy.

No Node.js, no GitHub, no build command required.

## Contents

- `index.html` — landing page, newest first
- `posts/` — one HTML file per article
- `assets/` — article cover thumbnails, inline images, videos, and embedded NFT thumbnails (downloaded from IPFS)
- `manifest.json` — machine-readable backup index
- `style.css` — site styles

## Source & license

- Source: fxhash GraphQL API (`{graphql}`)
- Original posts: <{profile}>
- Tezos NFT embeds (`tezos-storage-pointer`) in articles have been resolved to fxhash / objkt.com links with a representative thumbnail downloaded; the NFT artifacts themselves are not bundled.

## Regenerate

Produced by [fxhash-articles-backup](https://github.com/javaing/fxhash-articles-backup).
""",
}


# ---------- CLI ----------
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="fxhash_backup",
        description="Back up an fxhash author's articles to a Cloudflare-Pages-ready zip.",
    )
    p.add_argument("username", help="fxhash username, e.g. aquaponics.kana")
    p.add_argument(
        "-o", "--output-dir", default=".",
        help="Where to write the resulting zip (default: current dir)",
    )
    p.add_argument(
        "-l", "--lang", default="auto", choices=["auto", "zh-Hant", "en"],
        help="Site UI language. 'auto' picks zh-Hant if articles contain "
             "substantial Chinese, otherwise 'en'. (default: auto)",
    )
    p.add_argument(
        "--insecure", action="store_true",
        help="Disable TLS certificate verification. Use only behind a corporate "
             "MITM proxy that re-signs HTTPS traffic with an untrusted CA.",
    )
    args = p.parse_args(argv)
    if args.insecure:
        import ssl
        ssl._create_default_https_context = ssl._create_unverified_context
        safe_print("      [warning] TLS certificate verification disabled (--insecure)")
    build(args.username, Path(args.output_dir).resolve(), lang=args.lang)
    return 0


if __name__ == "__main__":
    sys.exit(main())
