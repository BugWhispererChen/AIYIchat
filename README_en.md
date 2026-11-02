<p align="center">
  <a href="README.md">中文</a> · <strong>English</strong>
</p>

<p align="center">
  <img src="assets/readme/hero.svg" alt="AIYIchat — an AI companion that lives inside QQ and WeChat" width="100%">
</p>

<p align="center">
  <a href="https://github.com/BugWhispererChen/AIYIchat/releases/tag/v1.0"><img src="https://img.shields.io/badge/Download_V1.0-Windows%20%7C%20Linux-blue?style=flat-square&logo=github" alt="Download V1.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.11-blue.svg" alt="Python 3.11"></a>
  <a href=".github/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-ruff%20%7C%20mypy%20%7C%20pytest-brightgreen.svg" alt="CI"></a>
  <a href="https://github.com/BugWhispererChen/AIYIchat/stargazers"><img src="https://img.shields.io/github/stars/BugWhispererChen/AIYIchat?style=social" alt="GitHub stars"></a>
</p>

# AIYIchat (爱易聊)

> An AI companion that lives inside **QQ and WeChat** — the two dominant messaging apps in China.
> It **remembers what you told it, reads your mood, and reaches out to you first**.
> It understands your voice notes, looks at your photos, replies in a voice it learned from you,
> and sends the right meme at the right moment.
> 11 preset companion personas out of the box — or the one you build yourself.

**No app to install**: add a friend on QQ or WeChat and the conversation starts.
**No team required**: MIT licensed, one person and one machine is enough. Windows is a
double-click installer that ships its own database; Linux is one script. Install it and you
already have 11 personas and a full meme library, ready to chat.

> Most AI companion products spend their effort on "a stronger model". AIYIchat bets on something else:
> **the feeling of being accompanied comes from the layers around the model, not from the model itself** —
> how memory is retained, how a mood is caught, when to speak up, when to stay quiet,
> and how one reply should be split into several messages.
> If that framing interests you at all, **a Star is the most direct way to support this project**.

## 30 seconds of it

Below is an **illustrative exchange** — written to show what the system does, not a real user
transcript. It shows what one and the same sentence looks like when there is memory behind it:

```text
You     Interview's done. Feels like I bombed it.
AI      Oh — the one you spent two weeks preparing for?
AI      Don't sentence yourself yet. Which part got stuck?
You     The details on that last project. I couldn't answer.
AI      But you walked me through its data flow just last week
AI      Going blank in an interview is normal — it doesn't mean you weren't ready
AI      (voice note, 0:04)
AI      [sticker: a hand on your shoulder]
AI      Drop it for tonight. Go eat something warm; come back and vent at me if you want
```

Four things happen at once here: **the mood is caught before anything is analysed**, **a detail from
last week is retrieved**, **it switches to voice**, **and it sends a meme chosen by tone**.
The rest of this document takes them apart.

---

## Contents

1. [Highlights](#highlights)
2. [What is different here](#1-what-is-different-here)
3. [Why pick this one](#2-why-pick-this-one)
4. [Quick start (V1.0 packages)](#3-quick-start-v10-packages)
5. [Honest caveats](#honest-caveats)
6. [Tech overview](#tech-overview)
7. [Contributing](#contributing)
8. [Compliance & responsibility](#compliance--responsibility)
9. [Asset credits](#asset-credits)
10. [Roadmap](#roadmap)
11. [Disclaimer](#disclaimer)
12. [Security disclosure](#security-disclosure)
13. [License](#license)

---

## Highlights

| What you get | What that actually means |
|---|---|
| **Zero install** | Chat by adding a friend on QQ / WeChat — no new app. One system serves both platforms, with multiple bots and accounts online at once. |
| **It really remembers** | Five-tier memory: sedimentation → semantic recall → governance & dedup → reflection → relationship graph. Something you mentioned in passing last week is still there today. |
| **Proactive, with manners** | Starts conversations at appropriate moments and backs off when ignored. Do-not-disturb windows are configurable, and each persona has its own daily routine. |
| **Human, not model-shaped** | A deterministic "AI-tone" gate before sending, replies split at real typing rhythm, and never three paragraphs where one line will do. |
| **Four senses wired in** | Hears voice notes, reads photos, replies with preset or cloned voices, and draws you a picture when the conversation calls for it. |
| **Swap personas freely** | 11 preset companions + a guided admin wizard + cloning from your **own** chat logs and audio (matches writing style *and* voice). |
| **Measurable quality** | An offline evaluation harness: LLM-as-a-judge + deterministic metrics + pairwise GSB comparison. Whether a change helped is a number, not a vibe. |
| **Affordable for one person** | Single-machine deployment; the Windows package bundles the database and audio toolchain. Config changes take hot effect — no restart. |
| **Personal use *or* running a site** | A complete end-user web stack (self-registration / channels / services / account centre), off by default, and it does not touch existing paths. |

---

## 1. What is different here

### 1.1 Memory: five tiers, not "stuff more into the context window"

Ordinary chatbots forget as soon as the session ends. Here memory is a chain you can inspect and
tune tier by tier — each tier can be toggled and evaluated independently:

| Tier | What it does | What you notice |
|---|---|---|
| **① Sedimentation** | Rolling history window plus auto-summary as the conversation grows; key points are written out as facts | "That small thing I mentioned in passing last week — it still remembers" |
| **② Recall & layered injection** | Stable facts stay in the system-prompt prefix to hit the cache; per-turn semantic recall is injected afterwards | It understands you better over time, while responses stay fast |
| **③ Governance** | Refers to the same thing the same way, so one fact isn't re-learned as "new" repeatedly; point-in-time events like "interview tomorrow" expire and stop being mentioned | Memory doesn't get dirtier with use, and it doesn't misquote stale facts |
| **④ Reflection** | Once facts cross a threshold they're distilled into higher-order insights — each traceable to at least two source facts, individually revocable in the memory page | It doesn't just keep fragments; it concludes "you've been under a lot of pressure lately" |
| **⑤ Relationship graph** | Optional embedded graph database holding "who is related to whom"; without it the system degrades to one-hop queries with no feature loss | It can connect your friend's name to your mother's job |

Tier ② exists because of a bug we hit in production. Semantic recall computes a query vector every
turn, and when the embedding service slowed down it swallowed over a minute of latency. The whole
recall path now runs under a shared timeout budget with a circuit breaker — **a slow success counts
as a failure**. On timeout it drops only that turn's semantic recall and injects the stable layer.
**It never blocks the conversation.**

### 1.2 Emotion: catch the mood before composing the sentence

Every incoming message is classified for emotion (11 label classes). The signal feeds both the
prompt and the prosody parameters of any voice reply — gentle when you're down, playful when you're
good, and the tone of the voice note actually matches the text.

Two details make it feel less like a tool:

- **It doesn't over-read short messages.** "Mm", "oh" and similar fall below a character floor and
  skip the extra model call, so a single interjection never gets a manufactured psychological profile.
- **Reaction before analysis.** When you share good news or vent, it responds emotionally first and
  asks for details afterwards — rather than immediately starting to analyse you.

### 1.3 Proactivity: it reaches out, and it also knows when to stop

The scheduler picks its moments using conversation history and time awareness. It is not a cron blast:

- **Do-not-disturb windows** are configured per service and per identity, so it won't wake you at 3am.
- **It restrains itself.** After several unanswered proactive messages it stops sending and records
  why — avoiding the "AI chasing you asking *are you there*" cringe.
- **Proactive content isn't just "drink some water".** Trigger points are proportionally replaced with
  real material: it fetches the day's trending topics and rewrites them in-character, or produces
  universal trivia (inventing news is explicitly forbidden). If fetching fails it falls back to trivia —
  **it would rather say less than make up news**.
- **Personas have their own routine.** Schedule, time-of-day and reply style are linked, so you never
  get the "just finished a run" persona replying instantly at 3am.

### 1.4 Human-likeness: deterministic guardrails, not hoping the model behaves

Prompts alone don't make an AI feel human. A set of deterministic rules with **zero extra model cost**
sits in the sending path:

- **Stream first, sanitize first.** On the web SSE stream, the model used to be converted token by
  token with all humanising cleanup queued *after* the full text — meaning the user saw everything and
  only the stored copy was clean. Cleaning now happens before anything is released:
  **every character you see on screen is the character that gets stored.** A half-emitted marker like
  `[sticker: a hug]` can never leak. The tradeoff is roughly 0.3–1s more time-to-first-token, accepted
  deliberately: slower is fine, breaking the illusion is not.
- **A pre-send "AI-tone" gate for Chinese.** Deterministic regexes catch giveaway phrasings —
  "Certainly!", "In summary", "I hope this helps" — and drop the whole sentence on a hit. The rule set
  was rewritten with reference to community tooling, and every rule has test cases pinning down its
  false-positive boundary.
- **Rhythm and message splitting.** Long replies are split into several short ones at human typing
  pace instead of dumping a wall of text; reply length has both a governance rule and a metric.
- **Repetition and loop detection.** A new reply that's too similar to the last two triggers a
  regeneration; several turns going in circles make it deliberately change the subject. No relying on
  the model's self-awareness.

### 1.5 Multimodal: all four paths live inside the conversation, not bolted onto an endpoint

| Sense | What it does |
|---|---|
| **Hear** (ASR) | You send a voice note, it understands — transcription feeds the same conversation pipeline. |
| **Speak** (TTS) | Voice replies using preset official voices or a voice cloned and bound to you. |
| **See** (Vision) | Photos are understood in context, including images inside memes. |
| **Draw** (ImageGen) | Generates images from the live conversation on request — and can edit them. |

Three features that do most of the humanising:

- **Voice cloning**: upload a short audio clip to register a personal voice, then use it directly for
  voice replies — **matching words *and* matching voice**.
- **Dynamic voice window**: after you send a voice note and it answers in voice, that conversation
  stays in voice mode for a randomized window (5–30 minutes by default). **Even if you switch back to
  typing, it keeps replying in voice.** Like two people sliding into voice without either announcing it.
- **It can meme-fight**: the built-in sticker library is matched by semantic vector search, and **if
  nothing fits, it sends nothing**. When the mood is right it throws a meme unprompted; if you only
  send a meme it will probabilistically answer with one back. Sticker groups can be bound to
  individual personas.

### 1.6 Personas: 11 preset, build your own, or clone one you know

- **Ready to chat**: the lead companion 麦筱悠 (Mai Xiaoyou), 5 girlfriends — 苏晚晴 (Su Wanqing),
  林小满 (Lin Xiaoman), 顾清欢 (Gu Qinghuan), 唐可可 (Tang Keke), 江屿 (Jiang Yu) — and 5 boyfriends —
  陆知行 (Lu Zhixing), 秦烈 (Qin Lie), 沈砚 (Shen Yan), 许念安 (Xu Nian'an), 程叙 (Cheng Xu). Each has
  differentiated personality, writing style and character dimensions, is bound to a distinct official
  voice, and ships with its own portrait.
  All content is Chinese-language.
- **Build your own**: the admin wizard takes a name and a description, has the model draft persona /
  speaking style / dialogue strategy, then you attach a portrait and a voice and confirm.
- **Clone**: import your **own** chat logs (own logs only, deletable at any time). The system analyses
  tone, vocabulary and rhythm in chunks and produces an editable persona file; combine it with voice
  cloning and you get the way someone you know actually talks. Clone fidelity has its own evaluation
  script and sample set.
- **One bot, many faces**: personas bind flexibly to bots and users and are resolved per conversation —
  the same bot can present a different personality to different people.

---

## 2. Why pick this one

### 2.1 Deployment friendly to the point of being foolproof

- **Windows**: one installer bundles the service panel, database, audio transcoding toolchain and HTTPS
  certificate — **no Docker, no Python, no command line**. Double-click and you have a local service
  with a GUI panel.
- **Linux**: unpack and run `./deploy.sh`. It generates a self-signed certificate, composes the
  containers and starts the database.
- **Source-only delivery**: the Linux package contains readable Python source (no compilation, no
  obfuscation) so you can audit and modify it. Packaging reverses the check to keep sensitive files out;
  certificates are generated at deploy time.
- **Hot config reload**: models, channel credentials and feature flags live in the database and take
  effect the moment you save — no container restart. Anything you configured in the admin UI survives
  upgrades instead of being reset to code defaults.

### 2.2 Quality is argued with data, not with "it feels better now"

- **Offline evaluation harness**: LLM-as-a-judge scoring + deterministic metrics (length distribution,
  AI-tone hits, memory consistency) + **pairwise GSB comparison** — the same input runs through the
  before and after versions and is judged Good / Same / Bad.
- Memory governance has dedicated statistics scripts, reply length has a reporting standard, cloned
  personas have their own evaluation entry point.
- Why that matters: **you can measure before rewriting a prompt and compare after**, instead of
  drifting into superstition.

### 2.3 One system can carry several "people" and several accounts

- Concurrent QQ bots and multiple WeChat accounts. When one inbound message contains text + image +
  voice segments, none of them overwrite each other — everything is stored and reviewable.
- **Per-identity quotas**: text is metered in tokens, voice in clips, images in pieces, and a failed
  synthesis is refunded automatically. Proactive messages pre-check quota before sending, so **the
  system never keeps incurring external cost after you've run out**.
- Media has per-account quotas (1 GB by default). Over quota, your own oldest media is evicted first
  and its references detached — shared sticker packs, public persona art and history referenced by
  other accounts are **never evicted**.

### 2.4 Works for personal use, and for running a site

- **Personal**: add a bot, bind a persona, set a quota. Private and done.
- **Running a site** (off by default; enabled by two switches; QQ / WeChat behaviour is completely
  unaffected): the full self-registration → admin approval → web login → streaming chat chain, with
  graphical CAPTCHA, per-IP sliding-window rate limiting, failed-login lockout, and session concurrency
  caps with sliding expiry. Users add their own QQ / WeChat / web channels from the browser (QR-paired
  credentials), create multiple chat services (service = identity × channel × persona, which decides who
  it plays and where memory and quota belong), and the chat page switches per service while keeping
  history and media. In-browser recording for voice input and persona-voiced replies are supported. The
  account centre covers password change, device management, quota view and account deletion.

### 2.5 Security and observability are built in, not on the roadmap

- The template admin password is **rejected at login** with a configuration prompt — since the template
  value ships in public source, copying it verbatim would hand the admin panel away for free.
- Login brute-force limiting works on both IP and username dimensions.
- C-end endpoints resolve identity from the session token and **do not trust client-supplied platform /
  bot IDs**. A new endpoint that fetches by ID without checking ownership is caught outright by a static
  guardrail test in CI.
- Password hashes never enter conversation context; channel credentials are exposed only as "configured
  or not" and never returned in plaintext; media links use non-enumerable capability URLs.
- Response filtering, content auditing, a metrics dashboard, health checks with breaker state, error
  aggregation, and per-stage timing logs — you can read which stage was slow straight out of the log.
  An "83 seconds for one turn" incident was located this way, down to the vector recall stage. Cutting a
  post-restart first turn from **88.1s to 6.4s** was only possible because the measurement existed first.
- Compliance is not deflected onto users: built-in filtering and auditing, registration forces a
  "I am at least 14" acknowledgement with the accepted terms version recorded, and real limitations such
  as rate limiting breaking under multi-instance deployments are documented rather than hidden.

---

## 3. Quick start (V1.0 packages)

**Grab V1.0 from [Releases](https://github.com/BugWhispererChen/AIYIchat/releases/tag/v1.0)**, or use
the third option below and build from source. Either way you only need two things:
**an Aliyun Bailian (DashScope) API key** — shared by text, voice and image — **and an admin password**.

| Artifact | For | Size | SHA-256 |
|---|---|---|---|
| `AIYIchat_Setup_v1.0_20261101.exe` | Windows 10 / 11 x64, single machine | ~110 MB | `f9f3f66775a6b310ea162c1b0548919a7d8485edef072b83e28a9b066357ece2` |
| `aiyichat-deploy-v1.0-20261101-linux.tar.gz` | Server / NAS / Docker | ~11.6 MB | `d5853bab941ab70c66a7267605c888fa9137c0742c527e22115c0c99a1a8f16d` |

> Verify (PowerShell): `Get-FileHash .\AIYIchat_Setup_v1.0_20261101.exe -Algorithm SHA256`
> Verify (Linux): `sha256sum aiyichat-deploy-v1.0-20261101-linux.tar.gz`
> Binaries are not distributed through Git for size reasons (excluded via `.gitignore`) — they are only
> attached to the Release.

### Option 1 — Windows installer (best if you don't want a command line)

1. Double-click `AIYIchat_Setup_v1.0_20261101.exe`; admin rights are required, and "start on boot" is
   optional. The installer bundles the database (registered as a Windows service, listening on
   `127.0.0.1` only), the audio toolchain and HTTPS certificates. **No Docker / Python / MongoDB needed.**
2. Edit `AIYIchatService\.env` in the install directory (under `Program Files` by default) and set
   `DASHSCOPE_API_KEY`.
3. Launch the "AIYIchat Service Panel" shortcut — opening the panel **starts the service automatically**.
   It shows service state, database connectivity, uptime, URLs and CPU/RAM, with a scrolling log;
   closing the window minimises it to the tray and keeps running. After editing `.env`, press Stop
   then Start in the panel to apply.
4. **Admin password**: if `ADMIN_PASSWORD` in `.env` is empty or still the template value, the first
   start generates a random strong password and **prints it exactly once in the panel log** — copy it
   immediately.
5. Open `https://localhost:8443/admin/login` (or click "Open admin panel" in the tray panel; the Windows
   default port is **8443**, Linux uses 443). The self-signed certificate will warn — proceed anyway.
   LAN IPs are included in the certificate SAN, so other devices on your network can reach it via
   this machine's IP.

Uninstalling asks whether to delete database data (chat logs, personas and other business data).
Choose "no" and a reinstall picks up where you left off.

### Option 2 — Linux package (server / production)

```bash
tar -xzf aiyichat-deploy-v1.0-20261101-linux.tar.gz
cd aiyichat-deploy-v1.0-20261101-linux

cp .env.example .env      # set DASHSCOPE_API_KEY and ADMIN_PASSWORD
chmod +x deploy.sh
./deploy.sh               # HTTPS 443 + HTTP 80 by default (HTTP redirects to HTTPS); self-signed cert auto-generated
# ./deploy.sh --https-port 8443 --http-port 8080    # custom ports
```

Requires Docker 20.10+, Docker Compose 2.0+, and at least 2 GB free RAM.
Once deployed, open `https://<host>/admin/login` (append the port if customised).

### Option 3 — From source with Docker Compose

```bash
git clone https://github.com/BugWhispererChen/AIYIchat.git && cd AIYIchat/aiyichat
cp .env.example .env      # same two values: DASHSCOPE_API_KEY and ADMIN_PASSWORD
docker compose up -d --build
docker compose logs --tail=50 app
```

### The three things to do after install

1. **Add your key**: the first start derives five default model configurations from
   `DASHSCOPE_API_KEY` (text / text-to-image / speech synthesis / embeddings / image editing). To switch
   models or providers later, edit them on the admin "AI model config" page — saving applies hot.
2. **Add a friend**: in the admin panel under *account → channel management*, pair a QQ bot by QR code
   or log in a WeChat account. Credentials are stored in the database and hot-loaded —
   **no local config files at all**.
3. **Start chatting**: the 11 preset personas and the sticker library are loaded automatically at
   startup; bind a persona and talk.

The admin panel covers: dashboard, persona management, accounts & channels, conversation records,
memory management, media & AI model config, stickers, plan billing, safety filtering, feature lab,
web accounts, and hot config reload.

C-end web pages (optional, off by default): enable them on the admin "web accounts" page, then use
`/web/register`, `/web/login`, `/web/chat`, `/web/services`, `/web/account`. While the switch is off,
these routes and their APIs reject anonymous access.

Full environment variable list, directory layout and troubleshooting (port conflicts, certificate
trust, logs, database backups, restarts) live in [aiyichat/README.md](aiyichat/README.md).

---

## Honest caveats

Stated plainly, because these are the reasons a repository gets un-starred later:

- **QQ and WeChat are the point.** If you are not in China or don't use those two apps, the channel
  layer is the part that won't apply to you. Channel adapters normalise platform differences into one
  message format and everything downstream is shared, so another channel is a new adapter rather than a
  rewrite — but that work isn't done yet.
- **You need your own API key, and voice/image are Aliyun-bound today.** Chat and embeddings go through
  OpenAI-compatible endpoints and are provider-swappable. ASR, TTS and voice cloning currently call the
  DashScope SDK, so a Bailian key is required for those paths; image editing additionally recognises
  Gemini-native and OpenAI-compatible endpoints. No hosted demo exists — nothing runs on my server for you.
- **All persona content is Chinese.** The 11 preset personalities, prompts and meme library are written
  for Chinese conversation; the AI-tone guardrails are Chinese-specific regexes.
- **Rate limiting and CAPTCHA are single-instance in-memory.** A container restart clears them, and
  running multiple replicas means each counts independently — you need shared storage (e.g. Redis)
  before opening public registration. See the compliance section.
- **This is one person's project.** It is small, actively developed, and answers arrive on my own clock.

## Tech overview

Python 3.11 + FastAPI (fully async), MongoDB (async driver with a repository layer), Docker Compose
for single-machine orchestration. Models are reached through OpenAI-compatible APIs (Aliyun Bailian /
DeepSeek and similar, switchable); the admin UI is server-rendered with partial updates and
**requires no frontend build chain**. Platform differences (QQ WebSocket, WeChat iLink) are normalised
by adapters into one message format, after which every channel shares the same conversation pipeline.

Full stack, request path diagram, directory responsibilities, data model and key design tradeoffs:
**[docs/ARCHITECTURE.md](docs/ARCHITURE.md)** (Chinese).

## Contributing

Issues and PRs are welcome — including typo fixes in this README.

- **Ideas / bugs**: use the [issue templates](.github/ISSUE_TEMPLATE) (bug report and feature request).
  Before pasting logs, strip server addresses, keys and usernames — this is a public repository.
- **Local development**:
  ```bash
  cd aiyichat
  python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
  .venv/Scripts/ruff check app tools      # lint
  .venv/Scripts/mypy app                  # type check
  python tools/test/run_tests.py          # test suite
  ```
- **Before committing**: read [CONTRIBUTING.md](CONTRIBUTING.md) (commit message format
  `<type>: <summary>`, secret-leak final check, dependency vulnerability audit). This is a public
  repository — **never** commit server addresses, credentials or internal paths.
- **Adding a channel / a model**: a channel only implements the unified message interface at the
  adapter layer; a model only needs a credential group configured in the admin panel. Neither touches
  the conversation engine.

> If "companionship comes from the layers around the model" sounds like an interesting bet to you,
> **a Star** helps more people find it. To follow future releases, **Watch** the repo or bookmark
> [Releases](https://github.com/BugWhispererChen/AIYIchat/releases).

## Compliance & responsibility

This is an open-source technical project. Whoever **deploys** it (the individual or organisation
actually running it) carries the following obligations:

- **Generative AI filing**: offering the service to the public requires algorithm / foundation-model
  filing under China's *Interim Measures for the Management of Generative AI Services*.
- **Content safety liability**: the deployer is responsible for filtering and auditing AI-generated
  content. Built-in safety review is provided, but the primary responsibility stays with the deployer.
- **Minor protection**: this project ships emotional-companion personas, so the deployer must present
  minor-use restrictions prominently. When C-end web registration is enabled, the signup page enforces
  an "I am at least 14" acknowledgement and rejects under-14 users outright; the acknowledgement and
  the accepted terms/privacy versions are recorded. Deployers should separately assess additional legal
  duties before serving minors.
- **Registration review & anti-abuse**: registration requires manual admin approval by default;
  registration and login are protected by graphical CAPTCHA and per-IP sliding-window rate limiting,
  and a rejected username cannot be retried for 24 hours. When serving the public, keep review and
  action records to discharge content-safety duties.
- **Known scaling limit**: rate limiting and CAPTCHA are **single-instance in-memory** implementations,
  cleared on container restart. **With horizontal scaling each instance counts separately, so limiting
  and CAPTCHA verification stop working** — move to shared storage (e.g. Redis) before opening public
  registration on multiple instances.
- **Reverse proxy requirements**: rate limiting counts client IPs. Direct deployments use the TCP peer
  address; "trust proxy headers" may only be enabled once the proxy layer **overwrites**
  `X-Forwarded-For` with `$remote_addr`. It is off by default so a client cannot forge the header and
  bypass limiting.
- **No unlawful use**: do not use this project for fraud, harassment or invasion of others' privacy.

## Asset credits

- **Preset sticker library** (`aiyichat/data/stickers/`): from the open-source
  [ChineseBQB](https://github.com/zhaoolee/ChineseBQB) dataset. Much of the material originates from
  internet memes and its original authorship may be unclear — deployers should assess this before
  commercial use. Any item can be disabled or deleted on the admin "sticker management" page and
  replaced with your own material. See [NOTICE.md](aiyichat/data/stickers/NOTICE.md).
- **Preset persona portraits** (`aiyichat/data/preset_portraits/`): generated by this project's own image
  service from prompts in a seed script. Not real people, not third-party material.
  See [NOTICE.md](aiyichat/data/preset_portraits/NOTICE.md).
- **Third-party components bundled in the Windows installer** (audio toolchain and database server):
  licence notices are in `THIRD_PARTY_NOTICES.txt` inside the installer.
- **README hero image** (`assets/readme/hero.svg`): a pure vector diagram made for this project, no
  third-party assets.

## Roadmap

### Done (V1.0)

- QQ + WeChat dual-platform access, with multi-bot / multi-account management
- Persona system: 11 presets, admin-guided creation, style cloning from chat logs, voice cloning
- Long-term memory with automatic summary, semantic retrieval and layered injection (stable facts kept
  in the prefix for cache hits, this-turn recall appended afterwards)
- Memory governance and a reflection layer (reference normalisation, expiry of point-in-time facts,
  traceable higher-order insights)
- Optional relationship-graph enhancement; degrades automatically when disabled, with no feature loss
- Emotion detection with emotion-aware replies, topic and mood tracking
- Proactive scheduling: do-not-disturb windows, per-service / per-identity configuration, self-restraint
  when ignored, trending-topic and trivia sharing (auto-degrades when network fetching fails)
- Human-likeness pipeline: on the streaming path **sanitize before releasing** (what you see is what is
  stored), deterministic Chinese AI-tone gate, unified rhythm and message splitting, reply-length
  governance, short-but-good replies (good news and venting get a reaction first), persona routines,
  repetition and loop detection
- Four-way multimodal (hear / speak / see / draw), semantic sticker retrieval, dynamic voice window
- Full admin panel + hot config reload, per-identity quotas and plan billing
- C-end web accounts: self-service registration (approval / CAPTCHA / rate limiting / session governance)
  and account centre (password, devices, quota, deletion)
- C-end self-service channels and services (QR-paired QQ / WeChat onboarding, multiple web channels,
  cross-channel conversation continuation and history review)
- Web voice input and AI voice replies; streaming delivery of images / stickers / voice
- Streaming inbound media download with size limits, per-account media quota and shared-resource protection
- Offline conversation-quality evaluation (LLM-as-a-judge + deterministic metrics + pairwise GSB)
- Observability: system metrics dashboard, health checks and breaker state, error aggregation,
  per-stage timing logs
- GitHub Actions CI (lint / type check / tests)
- **V1.0 packages for both platforms**: one-click Windows installer (bundled database and GUI panel) +
  Linux source package (one script, with automatic static integrity and runtime usability checks after
  packaging)

### Planned (pick one up)

- More preset personas and a marketplace-style persona manager
- Colloquialisation post-processing and a demonstration corpus of good dialogue pairs
- Proactive-message delivery channel for C-end web accounts (proactive messages currently reach
  QQ / WeChat only)
- Email / SMS self-service password recovery, online top-up
- Shared rate-limit storage (Redis) for multi-instance deployments

## Disclaimer

All conversation content is generated by artificial intelligence, for entertainment and companionship
only. It is not medical or psychological advice, and an AI companion cannot replace human contact or
professional services. If you are in emotional distress, please contact a professional support service
(in China: the national psychological aid hotline **12356**). Minors should use it only under
guardian guidance.

Persona cloning is restricted to **your own** chat logs, and the related data can be deleted at any time.

## Security disclosure

Please report vulnerabilities through GitHub's private vulnerability reporting
(Security → Report a vulnerability) rather than in a public issue. See [SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE).
