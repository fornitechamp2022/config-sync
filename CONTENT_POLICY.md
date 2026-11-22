# Content policy

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
on a public Vercel Blob store with a rolling 7-day cleanup — not in git.
`.gitignore` explicitly blocks media, logs, temp files, state and local
credential files from ever being committed.

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
key, hashes (dedupe/perceptual), timestamps and resulting platform IDs. The
state ledger (private Netlify Blob, with Vercel Blob/local fallback) holds
these hashes/URLs/IDs only — never image, audio or video bytes.

## Attribution

Source attribution is recorded, not hidden:

- Every run writes a manifest (`out/*.manifest.json`) including the source
  outlet and article URL.
- The source URL and credit are included in the YouTube/Instagram platform
  description, so the published reel points back to the publisher.
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
- remove/expire the reel from hosted storage and request removal from the
  platform where feasible.

Because reference material is used transiently and only metadata is retained,
removing a source is fast and leaves no media behind in this repository.

## Fair use / commentary context

This is a non-commercial, automated commentary/summary tool. We use only the
minimum excerpt needed to identify and discuss a news item, add original
narration, translation, captions and visual composition, and attribute the
source. We respect publishers' rights and act on valid takedown requests.
Review usage rights before any commercial publication or redistribution.
