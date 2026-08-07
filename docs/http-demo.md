# http_demo.pdf — a story, written live, from inside a PDF

A standalone PDF that calls a language model over the network and displays the
result, in Adobe Acrobat. No folder-level scripts, no certificates, nothing to
install into Acrobat.

See [http-plan.md](http-plan.md) for why it is built this way. (That plan targets
baconipsum, which was the original proof of concept — the mechanism is identical,
only the relay's upstream changed.)

## How it works

```
http_demo.pdf                              relay (holds the api key)
  [topic: ....... (optional)]
  [Generate] ──POST─────────▶ /api/story ──┬─ starts job, replies in ms
             ◀── job id + WORKING ─────────┘        │
  ...every 4s:                                      ├──▶ ai.hackclub.com
  [poll]     ──POST job=xxx ─▶              ────────┘         │
             ◀── WORKING, or the story ◀───── job done ◀───────┘
  [status] [job] [story .........]         /crossdomain.xml
```

**Acrobat will not hold a `submitForm` connection open for a slow response.** The
first working version called the model inline, took 15–35s, and Acrobat dropped
the socket before the reply could be written — the relay saw
`ConnectionResetError: [WinError 10054]` after returning 200, and the PDF showed
nothing. There is no timeout setting to raise.

So the relay never calls the model from the request handler. It starts a
background job, replies immediately with a job id, and the document script
re-submits with that id every 4 seconds until the job reports done. Every
individual request now completes in **3–50ms**.

The `job` field is deliberately visible: if this breaks, whether the id survived
the FDF import is the first thing to check.

**Never inspect a field's value immediately after `submitForm()` to decide what to
do next.** The FDF reply has not been imported at that point, so you read stale
text. The first version scheduled its first poll only if the status already said
`WORKING`, which it never did that early — so polling never started, and every
click silently leaked a new job on the relay while the PDF ignored the results.
The first poll is now scheduled unconditionally, and `is_finished()` decides when
to *stop* (status begins `OK -` or `relay error`) rather than when to continue.

There is also a **Poll now** button. If `app.setTimeOut` ever fails to fire in
this context, that button still retrieves the result — and it is the quickest way
to tell the two failure modes apart.

`Doc.submitForm()` is the only network API Acrobat exposes to an ordinary
document — it is not security restricted (`Security = No`) and works in the free
Reader outside a browser. When the server answers with
`Content-Type: application/vnd.fdf`, Acrobat parses the FDF and imports the field
values **into the PDF that made the request**. That is the return channel.

The request is therefore proxied. That is not only a limitation — it is what
keeps the key safe. Anyone who opens a PDF can read every byte of it, so a key
embedded in the document is a published key. The PDF just asks for a story; the
relay holds the credential.

## The prompt

The relay sends a fixed prompt (`BASE_PROMPT`, defined in both relays — **keep
the two copies in sync**), plus the user's topic if they typed one, plus a short
random entropy token. The token exists because the prompt asks the model to be
random, and because it stops any layer in between from serving a cached
completion.

## The model, and two traps

The default is **`qwen/qwen3-32b`**, in both relays. Measured at 13–20 seconds
per story. Speed is the reason for the choice, not quality: a Vercel Hobby
function is killed at 60s and `waitUntil` work dies with it, so anything slower
leaves jobs that never finish.

**Reasoning models are a trap here.** `~deepseek/deepseek-v4-flash-latest` and
friends burn 300–900 tokens thinking before writing a single word, and take
anywhere from 16 seconds to over three minutes for the same prompt. If
`max_tokens` does not cover the thinking as well as the story, the API returns
`finish_reason: "length"` with **`content: null`** — no story, no error, nothing.
`MAX_TOKENS` defaults to 3000 rather than the ~500 a 300-word story needs, and
both relays raise a message naming this cause instead of failing on a null.

**The leading `~` is part of the model ID**, not a typo — it marks a floating
alias on the proxy. `deepseek/deepseek-v4-flash-latest` without it returns
`is not a valid model ID`.

Override with `HACKCLUB_AI_MODEL` or `HACKCLUB_AI_MAX_TOKENS`. `POLL_LIMIT` in
`httpdemo.js` covers five minutes, so a slow model still works against the local
Python relay, which has no time ceiling — just not on Vercel Hobby.

## Setup

```sh
cp .env.example .env
# put your key in HACKCLUB_AI_KEY
```

`.env` is gitignored. A real environment variable overrides the file, so CI and
one-off overrides still work.

```sh
# terminal 1 - the relay
.venv/Scripts/python.exe tools/dev_relay.py 8000

# terminal 2 - a pdf pointed at it. note out/, not public/
.venv/Scripts/python.exe gen_http_pdf.py out/http_demo.pdf local
```

Open **`out/http_demo.pdf`** in Acrobat (not a browser), optionally type a topic,
and click *Generate (HTML)*. Acrobat may ask once whether to allow the document to
contact the host — choose **Allow** and tick *Remember my action for this site*.

Keep the two builds straight:

| File | Relay | Purpose |
|------|-------|---------|
| `public/http_demo.pdf` | `https://slop.alexvd.dev/api/story` | committed, deployed |
| `out/http_demo.pdf` | `http://127.0.0.1:8000/api/story` | local testing, gitignored |

The relay serves the same `public/` directory Vercel does, so
<http://127.0.0.1:8000/> shows the real landing page. Its download link therefore
hands you the *production* PDF, which will not talk to your local relay — for
local testing open `out/http_demo.pdf` from disk instead.

Routes: `/`, `/http_demo.pdf`, `/crossdomain.xml`, `POST /api/story`. Static files
are read off disk per request, so a rebuild needs no restart.

The relay logs each parsed submission, which tells you what Acrobat actually
sent.

## Deploying

Deployed on **Vercel** at `https://slop.alexvd.dev`.

| Path | What |
|------|------|
| `api/story.js` | the serverless relay |
| `public/index.html` | landing page |
| `public/crossdomain.xml` | Adobe's policy file (required — see below) |
| `public/http_demo.pdf` | the built PDF, **committed** so Vercel can serve it |
| `vercel.json` | `maxDuration` and the policy file's Content-Type |

Setup:

1. Add the **Upstash Redis** (or Vercel KV) integration to the project. It sets
   `KV_REST_API_URL`/`KV_REST_API_TOKEN`; the code also accepts the
   `UPSTASH_REDIS_REST_URL`/`_TOKEN` pair.
2. Set `HACKCLUB_AI_KEY` in the project's environment variables.
3. Point `slop.alexvd.dev` at the project.

Rebuild the PDF after any change to `httpdemo.js` or `gen_http_pdf.py`, and commit
it — it is a build artifact, but Vercel deploys from git and has no Python
toolchain here:

```sh
.venv/Scripts/python.exe gen_http_pdf.py public/http_demo.pdf
```

**The PDF must be built against the host it will actually talk to.** The URL is
baked in at generation time, so one built with `local` will keep trying
`127.0.0.1` on whoever opens it.

### Two things that will silently break it

**`/crossdomain.xml` must be served, with the right Content-Type.** A PDF opened
from disk has **no origin**, so Acrobat treats every response as cross-domain and
drops it unless the host explicitly permits it. Vercel would serve `.xml` as
`application/xml`; Adobe expects `text/x-cross-domain-policy`, which is why
`vercel.json` sets it. Verify after deploying:

```sh
curl -i https://slop.alexvd.dev/crossdomain.xml
```

**Generation must finish inside the function's time limit.** Vercel Hobby caps an
invocation at 60s, and `waitUntil` work dies with the function — so a generation
slower than that leaves a job that never completes. Hence the fast non-reasoning
default model (`qwen/qwen3-32b`, measured 13–20s) and `JOB_DEADLINE = 55`, which
reports a clear error rather than letting the PDF poll for five minutes. A
reasoning model here would take 15–180s and fail unpredictably.

Vercel's production branch defaults to the repository's default branch. Make sure
that is the branch this code actually lives on, or the deploy will serve nothing
and `/api/story` will 404.

## Notes and gotchas

- **Use `127.0.0.1`, not `localhost`.** On Windows `localhost` resolves to `::1`
  first; with the dev relay bound to IPv4 only, every request pays a ~2 second
  fallback penalty. Same relay over `127.0.0.1` answers in 3ms.
- **Acrobat freezes briefly on each poll.** `submitForm` blocks the UI, but each
  request is now milliseconds rather than the whole generation, so it is a flicker
  instead of a hang. The status field counts elapsed seconds so there is visible
  progress.
- **Never let the relay block on the model.** See the architecture note above —
  this is the single thing most likely to break if the relay is rewritten.
- **Two Generate buttons on purpose.** Adobe's docs are inconsistent about
  whether a `cSubmitAs: "HTML"` submit can receive an FDF reply, so both
  encodings are wired up and the relay accepts either. Once you know which works,
  keep that one and drop the other.
- **Chrome will not work.** PDFium implements no network API at all. The PDF is
  Acrobat-only by nature, not by choice.
- **Only `/Fields` injection is used.** An FDF reply can also carry
  `/JavaScript`, but Enhanced Security blocks that unless the file is in a
  privileged location, so nothing here depends on it.
- **The trust prompt can blank the fields.** Adobe documents that accepting trust
  via the yellow message bar reloads the document. Click Generate again.
- **Errors are shown, not swallowed.** A missing key or an upstream failure comes
  back as an FDF that fills `status` with the reason.

## Field flags: the Required trap

`/Ff` flags are **1-based bit positions**, so bit 1 is value `1` and bit 2 is
value `2`:

| Bit | Value | Flag |
|----:|------:|------|
| 1 | 1 | ReadOnly |
| 2 | 2 | **Required** |
| 13 | 4096 | Multiline |
| 17 | 65536 | Pushbutton |

Writing `Ff = 2` intending ReadOnly actually marks the field **Required**. Acrobat
then refuses to submit the form at all, with:

> At least one required field was empty.

This is easy to miss because Chrome ignores it entirely — upstream linuxpdf set
`Ff = 2` on all 310 of its fields and nothing ever complained, since that
document never submits a form. Use the `FF_*` constants in `pdfform.py`; nothing
should ever set `FF_REQUIRED`.

Note also that the fields the FDF reply writes into are left **writable**. Not
because ReadOnly is known to block an FDF import, but because it is one fewer
thing that can, and it lets you select and copy the story out.

## FDF string encoding

Model output is full of em dashes and smart quotes, which a PDF literal string
cannot carry — the field's font is WinAnsi. So `pdf_text_string()` emits:

- a **literal** string `(...)` when the text is pure ASCII, so the FDF stays
  readable while debugging — with `\`, `(` and `)` escaped, and newlines emitted
  as the two-character `\r` escape (a raw CR byte inside a literal string is read
  by the spec as `\n`, silently changing the line separator);
- a **UTF-16BE hex** string `<feff...>` otherwise, where the leading BOM is what
  marks it as Unicode.

The Python and JavaScript implementations are verified to produce byte-identical
output for backslashes, parens, LF, CRLF, non-ASCII and astral-plane characters.

## AcroForm prerequisite

Upstream linuxpdf emitted no `/AcroForm` dictionary. Chrome tolerates that
and scavenges widgets off the page's `/Annots`; Acrobat resolves `getField()`
names **only** through `/AcroForm /Fields`, so every field was invisible to
JavaScript there. `attach_acroform()` in `pdfform.py` fixes it.

Three things that bite when doing this with pdfrw:

- `PdfWriter.addpage()` resets `writer._trailer`, so `/AcroForm` must be attached
  **after** `addpage()` or it is silently discarded.
- pdfrw has no boolean type — `NeedAppearances=True` serialises as `True`, which
  is invalid PDF. Use `PdfObject("true")`.
- Fields must be `indirect = True`, or pdfrw duplicates each one that appears in
  both `/Annots` and `/Fields` (a `log.warning` only), producing two widgets
  sharing one name.

Note also that upstream linuxpdf reaches fields via `globalThis.getField()`.
That works in PDFium but not in Acrobat, where `getField` is a `Doc` method —
hence `var DOC = this;` at the top of `httpdemo.js`.
