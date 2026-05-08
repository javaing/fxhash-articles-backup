# fxhash-articles-backup

Back up any fxhash author's articles into a single self-contained zip — ready
to deploy as a static site on Cloudflare Pages, Netlify, GitHub Pages, etc.

The output zip contains:

- `index.html` — landing page listing every article, newest first
- `posts/<date>-<slug>.html` — each article rendered as standalone HTML
- `assets/` — article cover thumbnails, inline images / videos, and NFT
  embed thumbnails, all downloaded from IPFS
- `manifest.json` — machine-readable index of every article (includes
  IPFS hashes so the originals can always be located on-chain)
- `style.css`, `README.md`, `AGENT.md` — site assets and deploy notes

It also handles the fxhash-specific markdown directives:

- `::tezos-storage-pointer[]{...}` (NFT embeds) → resolved via the fxhash
  GraphQL API (for fxhash projects) or the public TzKT API (for any other
  Tezos NFT), then rendered as a clickable thumbnail figure
- `::video[]{src=...}` → rendered as a `<video controls>` tag

Site UI supports **English and Traditional Chinese**. The chrome (back
button, footer, "View on fxhash", etc.) is auto-picked from the article
content, or you can force it with `--lang en` / `--lang zh-Hant`.

---

## Use it (two ways)

### A. GitHub Actions — easiest, no install needed

1. **Fork** this repository.
2. Open the **Actions** tab on your fork. If prompted, enable workflows.
3. Pick **Backup fxhash articles** in the left sidebar.
4. Click **Run workflow**, type the fxhash username (e.g. `aquaponics.kana`),
   and **Run workflow** again.
5. When the run finishes, scroll to the bottom of the run page and download
   the `<username>-fxhash` artifact. Inside is the zip you can drop into
   Cloudflare Pages / Netlify / GitHub Pages.

### B. Run locally

Requires Python 3.10+.

```bash
git clone https://github.com/javaing/fxhash-articles-backup.git
cd fxhash-articles-backup
pip install -r requirements.txt
python fxhash_backup.py <username>
```

The zip is written to the current directory as `<username>-fxhash.zip`.

Optional flags:

```bash
python fxhash_backup.py <username> --output-dir ./out
python fxhash_backup.py <username> --lang en        # force English UI
python fxhash_backup.py <username> --lang zh-Hant   # force Traditional Chinese UI
```

---

## Deploy the zip

1. Unzip it.
2. Open Cloudflare **Workers & Pages → Create → Pages → Upload assets**.
3. Drag the unzipped folder in. Click **Deploy site**.

No Node.js, no build command, no GitHub repo on Cloudflare's side. The same
folder works on Netlify (drag-drop) and GitHub Pages (commit + enable Pages).

---

## How it works

| Step | Source |
| --- | --- |
| Article list & body | fxhash GraphQL — `https://api.fxhash.xyz/graphql` |
| Cover thumbnails & inline images | IPFS gateways (nftstorage, cloudflare, ipfs.io, dweb.link, cf-ipfs) |
| fxhash NFT embed metadata | fxhash GraphQL `generativeToken(id:…)` |
| Other Tezos NFT embed metadata | TzKT public API — `https://api.tzkt.io/v1/tokens` |

Everything the script touches is public; no API key or login required.

---

## Limitations

- The script saves a **representative thumbnail** for each NFT embedded in an
  article, not the live generative artifact. If the original NFT is removed
  from IPFS / the contract storage, the embed degrades to a link only.
- IPFS gateways occasionally rate-limit. The script tries five gateways in
  sequence; transient failures are reported but don't stop the run.
- Articles authored on multi-author / collab accounts are listed under
  whichever single author the fxhash GraphQL API exposes.

---

## License

MIT — see [LICENSE](LICENSE).

The articles you back up belong to their authors; this tool only makes a
copy easier to host. Respect the original authors' wishes if they ask you
to take a backup down.
