# YT Bolly — content policy

This repository is **code only**. It is published on public git hosts (GitHub,
and may be mirrored to GitLab) as a software project — not as a media library.
The material below explains what is and is not stored here, how third-party
news content is used, and how rights holders can reach us.

## What is in the repository

- Python source (`generate.py`, `pilot.py`, `pipeline/`), tests (`tests/`),
  configuration (`config.yaml`), docs, the GitHub Actions workflow, and
  bundled open-source fonts (`assets/fonts/`, SIL Open Font License).
- Source names, feed URLs and output settings in `config.yaml`.

## What is **not** in the repository

We do **not** commit and never have committed:

- **Article bodies** or full text scraped from publishers.
- **Images / stills** (headline `og:image`, collage frames, screenshots).
- **Audio** (TTS narration, music) or **video / MP4** renders.
- **Credentials, tokens, API keys or any secret** — real or example values.
- Generated state ledgers or per-reel manifests.

All generated media is written to ignored local directories (`work/`, `out/`)
and, in CI, to short-lived GitHub Actions artifacts. Finished MP4s are hosted
on the **Netlify Blob store `reels-media`** — not in git and not in the static
`public/` folder — with a rolling **7-day cleanup**. GitHub Actions artifacts
(MP4, manifest and QA dump) are retained for only **2 days**. `.gitignore`
explicitly blocks media, logs, temp files, state and local credential files
from ever being committed.

### Hosted-media retention and public access

- The finished MP4 is uploaded with the Netlify CLI to `reels-media` under
  `reels/<ascii-reel-id>.mp4` and served from the public URL
  `https://bollywood-reel-generator.netlify.app/r/reels/<ascii-reel-id>.mp4`.
  A `/r/*` redirect sends requests to a serverless Function that streams the
  object with HTTP Range support.
- Anyone with the link can view the MP4 while it exists; it is not indexed as a
  media library and there is no directory listing. After each successful
  upload, blobs older than **7 days** are deleted (the just-uploaded object is
  always protected). Expiry is therefore automatic, not permanent hosting.
- A Vercel Blob store (`reels-media`) can be used as an optional fallback and
  applies the same 7-day rolling cleanup; it is not the active host.
- Local `work/` / `out/` copies are deleted after rendering in CI; the 2-day
  artifact window exists only for debugging failed runs.
- The private state ledger (`reel-state`) keeps only metadata records and is
  pruned after **30 days**; no image, audio or video bytes are ever stored there.

### Buffer posting and retention

Publishing is **Buffer-first**: a reel is handed to Buffer's official API with a
long-lived personal API key (no monthly login and no Meta/Google app
verification), and Buffer delivers it to the connected YouTube/Instagram
channels. Buffer hosts the scheduled/queued post under **Buffer's own retention
policy**; once published, the YouTube/Instagram reel is governed by those
platforms' retention and visibility rules. **No media is committed to this
repository** at any point — the reel is generated in ignored local/CI
directories, optionally mirrored to the 7-day `reels-media` blob, and posted via
official APIs. Posts are deliberately paced (at most 2/day, at least 3 hours
apart, varied stories; no mass posting, bots or session cookies), which reduces
risk but cannot guarantee a platform will never limit reach. Our own copies
expire on the schedules above regardless of what a platform retains.

## How third-party content is used

The generator discovers entertainment-news items through RSS and transforms a
small amount of third-party material into a **new, transformational
news-commentary reel**:

- It reads a headline, a short lead and a few article paragraphs **transiently
  in CI memory** to build a deterministic English script. Nothing is stored.
- A free machine-translation endpoint renders the script into **Hindi
  narration**, and a neural TTS voice supplies a brand-new spoken performance.
- The script is re-rendered as **Hinglish karaoke captions** over an original
  vertical collage and original motion graphics.
- The result is commentary/summary: original narration, original edit, original
  visuals composition — not a repost or a copy of the source article.

We keep only **metadata** about a source: canonical URL, a normalized title
key, hashes (dedupe/perceptual), timestamps, the public `/r/` MP4 URL and
resulting platform IDs. The state ledger (private Netlify Blob store
`reel-state`, with Vercel Blob/local fallback) holds these hashes/URLs/IDs only
— never image, audio or video bytes. Source URLs are also written into each
manifest and into the platform descriptions.

## Attribution

Source attribution is recorded, not hidden:

- Every run writes a manifest (`out/*.manifest.json`) including the source
  outlet and article URL.
- The source URL and credit are included in the platform description (including
  posts delivered through Buffer), so the published reel points back to the
  publisher.
- Feed/output settings and `preferred_sources` in `config.yaml` make the
  sourced outlets explicit.

## Takedown and contact

If you are a publisher, rights holder or anyone concerned about a referenced
headline, image or reel, please contact the repository maintainers **privately**
through GitHub's "Report a vulnerability" / security contact, using the private
channel described in [SECURITY.md](SECURITY.md) (do not post secrets publicly).
Include the article URL, the outlet and the reel/platform URL.

We will, on request and at our discretion:

- add the source domain or URL to `skip_url_patterns` / blocklists so it is no
  longer ingested;
- remove or expire the relevant metadata record from the state store;
- delete the MP4 from the Netlify `reels-media` Blob store (or let the 7-day
  rolling cleanup expire it) so the public `/r/` URL stops serving it, and
  request removal from YouTube/Instagram where feasible.

Because reference material is used transiently and only metadata is retained,
removing a source is fast and leaves no media behind in this repository. Hosted
MP4s expire within 7 days even without a request, CI artifacts within 2 days and
state metadata within 30 days; Buffer/platform retention for already-published
reels is governed by those services and may be requested from them directly.

## Fair use / commentary context

This is a non-commercial, automated commentary/summary tool. We use only the
minimum excerpt needed to identify and discuss a news item, add original
narration, translation, captions and visual composition, and attribute the
source. We respect publishers' rights and act on valid takedown requests.
Review usage rights before any commercial publication or redistribution.
