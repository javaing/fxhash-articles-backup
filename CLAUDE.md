# CLAUDE.md — fxhash-articles-backup

Project memory for future Claude sessions. Not user-facing — see `README.md`
for that.

## What this is

CLI that produces a **self-contained static-site zip** of an fxhash author's
articles (or a collector's holdings), ready to drop onto Cloudflare Pages,
Netlify, GitHub Pages, etc. All upstream data is fetched from public APIs;
no auth.

The "output format" mirrors Matters.town's `matters-lifeboat/v1` zip
(`index.html` + `posts/` + `assets/` + `style.css` + `manifest.json`). That
was the user-provided reference. Keep that shape if extending.

## Repo layout

```
fxhash_backup.py            single-file CLI (only source file)
requirements.txt            just `markdown` (pure-python md→HTML)
README.md                   user-facing docs (EN, zh-TW friendly)
LICENSE                     MIT
.gitignore                  excludes *.zip, .venv, etc.
.github/workflows/backup.yml workflow_dispatch with username + options
                             → uploads zip as artifact
```

GitHub: <https://github.com/javaing/fxhash-articles-backup>

## Pipeline (in `build()` inside `fxhash_backup.py`)

1. **Fetch article stubs** — `gql_user_articles(name)` for `author` mode,
   `gql_user_owned_articles(name)` for `owned`, both deduped for `all`.
2. **Fetch each article body** — `gql_article(slug)` with the canonical
   field set.
3. **Download IPFS assets** — three sub-passes via `fetch_to_assets()`:
   a. cover thumbnails (`thumbnailUri`),
   b. inline ipfs:// references in body (via `IPFS_REF_RE`),
   c. Tezos NFT embed thumbnails (resolved through fxhash GraphQL for the
      issuer contract `KT1BJC12dG17CVvPKJ1VYaNnaT5mzfnUTwXv`, otherwise via
      TzKT `https://api.tzkt.io/v1/tokens`).
4. **Render HTML + write manifest** — markdown→HTML with custom directive
   placeholders, rewrite ipfs:// to local `assets/...` paths (or to a
   gateway URL if no local copy), build `index.html`, write `manifest.json`,
   zip everything.

## CLI flags

Each flag is opt-in; nothing else changes from default behavior.

| Flag | Default | Effect | Code |
|---|---|---|---|
| `<username>` (positional) | required | fxhash user, e.g. `aquaponics.kana` (spaces OK, quote it) | `argparse` in `main()` |
| `-o/--output-dir DIR` | `.` | where to write the zip | `main()` |
| `-l/--lang {auto,zh-Hant,en}` | `auto` | site UI language; `auto` = `detect_lang()` based on CJK char density in bodies | `STRINGS` dict |
| `-x/--exclude SLUG` (repeatable) | none | drop a specific article. Accepts raw slug, slugified-filename stem, or full `{date}-{stem}` (paste from URL works) | `article_matches_exclude()` |
| `-m/--mode {author,owned,all}` | `author` | which articles to fetch | section above |
| `--max-asset-size SIZE` | none (no cap) | skip downloading any single IPFS asset bigger than SIZE; HTML falls back to `cloudflare-ipfs.com/ipfs/...`. Accepts `25M`, `30MB`, `1500000`, etc. | `AssetTooLarge` class + `fetch_ipfs(max_bytes=)` |
| `--insecure` | off | disables TLS verify (corp MITM proxy). Sets `ssl._create_default_https_context = ssl._create_unverified_context` | `main()` |

Output zip name:
- `author` mode: `<username>-fxhash.zip`
- `owned`: `<username>-owned-fxhash.zip`
- `all`: `<username>-all-fxhash.zip`

## fxhash markdown directives

Bodies contain non-standard `::name[]{attr="value"}` directives. Strategy:
**stash with placeholder → run md→HTML → swap placeholder back in**. See
`md_to_html()`. Each directive needs its own `stash_xxx` step.

Current handlers:

- `::tezos-storage-pointer[]{contract="…" path="ledger::N" / "token_metadata::N"}`
  — resolved during phase 3 into `NFT_INFO[(contract, token_id)]`, rendered
  by `make_render_tezos_pointer()` as a clickable thumbnail figure.
- `::video[]{src="…"}` — rendered as `<video controls playsinline>`. The
  src may be an `ipfs://` URI; rewriting to local/gateway happens via
  `rewrite_ipfs_in_md()` BEFORE md→HTML.
- `::embed-media[]{href="…"}` — rendered as a labelled link card. Platform
  detection in `render_embed_media()`: Twitter/X, YouTube, Spotify, Vimeo,
  Instagram, TikTok, generic. Tweet labels extract `@username` from the
  URL path.
- **Generic fallback** — any other `::xxx[]{…}` directive renders as a
  plain "📦 …" card so unknown directives never leak raw markup.

## i18n

Two languages: `zh-Hant` and `en`. All chrome strings live in `STRINGS`
dict at module top. Per-mode title/hero variants:
`site_title` / `site_title_owned` / `site_title_all`. Adding a language
= add a new key in `STRINGS`, `AGENT_TMPL`, `SITE_README_TMPL`, and add it
to the CLI `--lang` choices.

`detect_lang()` heuristic: counts CJK code points vs ASCII letters in
concatenated bodies. Rough but works.

## Asset handling rules

- `asset_map[ipfs_uri] = "assets/<prefix>-<sha1[:10]>.<ext>"`. Prefix is
  `thumb-`, `img-`, or `nft-<token_id>-`.
- Sha1 of the full `ipfs://CID/path` is the dedupe key — same URI in two
  articles = one file.
- Extension comes from `Content-Type` via `ext_from_ct()`. `.jpe` is
  normalized to `.jpg`.
- `oversize_uris: set[str]` tracks size-cap skips. **But network failures
  go through a separate `except Exception` branch and aren't counted here**
  — they fall back to gateway URLs the same way, but `oversize-skipped` in
  the final stats only counts true cap hits.
- Cap is enforced first via `Content-Length`, then by streamed read with a
  64 KB chunk loop (covers gateways that don't send Content-Length).
- IPFS gateways tried in order: `nftstorage.link`, `cloudflare-ipfs.com`,
  `ipfs.io`, `<cid>.ipfs.dweb.link`, `<cid>.ipfs.cf-ipfs.com`. The HTML
  gateway fallback always uses `cloudflare-ipfs.com`.

## GraphQL endpoints

| Purpose | URL | Notes |
|---|---|---|
| Articles, users, fxhash projects | `https://api.fxhash.xyz/graphql` (V3) | What this tool uses. `articles(take:N)` capped at `take<=50`. |
| Multichain / V4 fxhash data | `https://api.v2.fxhash.xyz/v1/graphql` | Not used — V3 covers Tezos-only article system. V4's `search` endpoints are flaky / blocked. |
| Generic Tezos NFT metadata | `https://api.tzkt.io/v1/tokens?contract=…&tokenId=…` | Used for non-fxhash NFT embeds. |

`PAGE_SIZE = 50` for paginated article fetching.

## Known good test users

- `aquaponics.kana` — Chinese-language author, 6 articles, exercises the
  `tezos-storage-pointer` directive heavily (~50 NFT embeds).
- `Strider5071` — English author, 3 articles, small (~1.6 MB zip). Good
  for fast smoke tests.
- `arttseng` (the project maintainer) — large `--mode all` test:
  ~80 articles, ~1.4 GB unbounded / ~300 MB with `--max-asset-size 25M`.
- `Elout de Kok` — exercises slug with apostrophes for `--exclude`.

## Common edits — recipes

**Add a new markdown directive.** Add a regex `XXX_DIR_RE`, a
`render_xxx_directive(m)` function, a `stash_xxx` step inside
`md_to_html()`. Style its output via a new CSS class in `STYLE_CSS`. If
its `src/href` references ipfs://, you don't need to download separately —
`rewrite_ipfs_in_md()` runs first and rewrites any ipfs:// inside the body
text.

**Add a new language.** Extend the `STRINGS` / `AGENT_TMPL` /
`SITE_README_TMPL` dicts with a new key. Add it to the `--lang` argparse
`choices=[...]`. Tweak `detect_lang()` if auto-detection should pick it.

**Add a new Tezos NFT thumbnail source.** Branch in the `for contract, token_id in pointers:` loop — currently it special-cases
`FXHASH_ISSUER_V2` and falls through to TzKT. Add additional contract IDs
above the TzKT fallback as needed.

**Tweak embed-media platforms.** All in `render_embed_media()` —
hostname-prefix matching + emoji + label string.

## Gotchas

- **Windows console codepage.** stdout may be cp950/cp1252; emoji and
  some CJK fail to encode. Use `safe_print()` everywhere, not bare
  `print()`. It catches `UnicodeEncodeError` and writes a sanitized copy.
- **Corporate TLS MITM.** Maintainer's network re-signs HTTPS with an
  untrusted CA. Python 3.14 rejects with `Basic Constraints of CA cert
  not marked critical`. `--insecure` flag works around this. Also affects
  `git push` — use `git -c http.sslVerify=false push`. Memory in
  `~/.claude/projects/.../MEMORY.md` documents this.
- **IPFS gateway truncation.** Some public gateways truncate at 30 MiB
  silently — files come back exactly `31457280` bytes with seemingly
  correct Content-Length. Treat any asset exactly 30 MiB as potentially
  corrupt.
- **fxhash issuer vs gentk contracts.** `KT1BJC12dG17CVvPKJ1VYaNnaT5mzfnUTwXv`
  is the fxhash issuer (one ID = one project, queried via fxhash GraphQL).
  `KT1U6EHmNxJTkvaWJ4ThczG4FSDaHC21ssvi` is GENTK v2 (one ID = one minted
  edition, queried via TzKT). Embeds can point at either; the conditional
  in the NFT-metadata loop distinguishes them.
- **`take` capped at 50.** fxhash V3 GraphQL rejects `take > 50`. The
  paginator just loops with `skip` until short page.
- **Slug edge cases.** Slugs may contain `(`, `)`, `.`, `'`. Filenames go
  through `slugify_filename()` which strips these; `--exclude` matches
  against multiple normalized forms so users can paste either form.

## Deploy targets

- **Cloudflare Pages** — 25 MB per file cap. Use `--max-asset-size 25M`.
  Upload via "Upload assets" (not the Git path) to skip build steps.
- **Netlify / Vercel static / GitHub Pages** — no per-file cap to worry
  about; unzip and use repo root as publish dir.

## Versioning

No tags / releases yet. Just `main`. Keep commit messages descriptive
(`Add --xxx flag: …` style) because that's the only changelog.
