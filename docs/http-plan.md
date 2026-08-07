# Dynamic HTTP requests from inside a PDF (Acrobat, zero-install)

## Context

`sloppdf` is a fork of ading2210/linuxpdf: a RISC-V Linux emulator compiled to asm.js and embedded in a PDF's JavaScript. Every "download" it performs is fake — `tinyemu/js/lib.js:70-98` overrides emscripten's `emscripten_async_wget3_data` to reject anything not starting with `file:///` and serve it from an in-memory `embedded_files` blob baked in at build time. There is no network primitive anywhere in the runtime.

The goal is to break that ceiling: a PDF that makes a **real, live HTTP request over the network** and displays fresh data — `https://baconipsum.com/api/?type=meat-and-filler` — with the result rendered in the document. Chrome's PDFium is out of scope (it implements no network API at all). Target is **Adobe Acrobat (Windows 26.1)**.

Requirement added during planning: the deliverable must be a **standalone PDF that works on someone else's machine** without installing anything into their Acrobat.

## What the research settled

**`Net.HTTP.request` is a dead end for a distributable PDF.** Three independent blockers: its doc note says it *"can only be made outside the context of a document (for example, in a folder level JavaScript)"*; it's flagged `Product = F` (forms rights), which the free Reader only gets via paid Reader Extensions; and it reportedly fails **even in the JS console**, which *is* a privileged context — meaning the restriction is a scope check, not a trust check. Notably it's the only such note in the reference that does *not* cross-reference "Privileged context." No certificate or privileged location has been shown to lift it.

**A self-installing PDF is impossible by design.** `exportDataObject` throws `NotAllowedError` whenever `cDIPath` is non-null, in every context, since Acrobat 6 — the path parameter was permanently removed. Adobe's security guide: *"sandboxed processes are specifically prohibited from writing to that folder."* And `.js` is on the attachment blacklist — such files *"cannot be saved or opened from the application."* Every disk-write API (`saveAs`, `exportAsFDF`, even `app.getPath`) is privileged-context-only. There is no bootstrap.

**`Doc.submitForm` is the way.** Quick bar: `3.01 | No | Security = No | Product = All`. Not security-restricted, available in free Reader, and *"Beginning with Adobe Reader 6.0, you need not be inside a web browser to call this method."* If the server replies with `Content-Type: application/vnd.fdf`, Acrobat parses the FDF and imports the field values **into the PDF that made the submit**. Enhanced Security explicitly permits this — it blocks FDF *"NOT returned as the result of a post from the PDF"*, and its rules table allows data injection when *"Data returned via a form submit"* and *"FDF has no /F or /UF key"* and *"cross-domain policy permits it."*

**Design constraint:** only `/Fields` injection is reliable. `/JavaScript` in an FDF response is *"Blocked if enhanced security is on and FDF is not in a privileged location"* — many people report it working anyway, but nothing here may depend on it.

**Honest framing:** the PDF makes a genuine live HTTP request over the network to our endpoint; that endpoint performs the baconipsum GET. It is proxied, not a direct browser-style call to a third-party API — no PDF can do the latter and remain distributable. Fresh data on every click, no user install.

## Architecture

```
out/http_demo.pdf                          Cloudflare Pages (existing deploy target)
  [type: meat-and-filler]  ──POST──▶  /api/bacon  ──GET──▶  baconipsum.com
  [Fetch] ◀────── FDF /Fields ───────      │
  [status ] [response ..............]      └── /crossdomain.xml  (domain="*")
```

Existing infrastructure carries this: `.github/workflows/deploy.yaml` already builds and publishes `out/` to Cloudflare Pages, so a Pages Function and a static policy file land on one origin with no new deployment target.

**Dev loop first.** Prove the FDF round trip against a stdlib Python relay on localhost before touching Cloudflare — no wrangler, no node, no cloud account in the debugging path. Port to the Worker once the mechanism is confirmed.

## Implementation

### 1. `gen_pdf.py` — make the output a valid AcroForm (prerequisite)

Acrobat resolves field names only through `/AcroForm /Fields`; PDFium is lenient and scavenges widgets off `/Annots`, which is why the current file works only in Chrome. Without this fix `getField()` returns `null`, the submit sends nothing, and the FDF import has nothing to bind to. Additive changes — existing callers keep byte-identical output.

- Extend `create_field(...)` with `multiline=False`, `readonly=True`, `da="/Helv 9 Tf 0 g"`. Set `annotation.DA = PdfString.encode(da)`, `annotation.F = 4` (Print), `annotation.indirect = True`. Compose `Ff` from flags: ReadOnly = 2, Multiline = **4096** (bit 13).
- Delete the dead appearance block at `gen_pdf.py:42-47` — it builds a local `appearance` dict that is never assigned to `/AP` and never returned (it also misspells `Subtype` as `SubType` and has no `stream`, so pdfrw would never emit it).
- Add `attach_acroform(writer, fields)`:

```python
writer.trailer.Root.AcroForm = PdfDict(
    Fields=PdfArray(fields),
    DA=PdfString.encode("/Helv 9 Tf 0 g"),
    DR=PdfDict(Font=PdfDict(Helv=IndirectPdfDict(
        Type=PdfName.Font, Subtype=PdfName.Type1,
        BaseFont=PdfName.Helvetica, Encoding=PdfName.WinAnsiEncoding))),
    NeedAppearances=PdfObject("true"),
)
```

Three pdfrw traps, all verified against `.venv/Lib/site-packages/pdfrw/pdfwriter.py`:
- **`addpage()` sets `self._trailer = None`** (line 268-269) and `trailer` is a lazily-rebuilt property — so `attach_acroform` must run **after** `addpage()` and **before** `write()`, or the `/AcroForm` is silently dropped.
- **pdfrw has no boolean type.** `NeedAppearances=True` serializes as `True`, which is invalid PDF. Use `PdfObject("true")` — pdfrw's own idiom, cf. `NullObject = PdfObject('null')` at `pdfwriter.py:26`.
- **Fields must be `indirect=True`**, else appearing in both `/Annots` and `/AcroForm /Fields` makes pdfrw *duplicate* them (a `log.warning` only) into two widgets sharing one `/T` — which Acrobat treats as broken.

Call `attach_acroform` from the existing `__main__` block too, so `linux.pdf` gains a valid AcroForm as a side benefit.

### 2. `tools/dev_relay.py` — local relay (stdlib only)

`http.server` + `urllib.request`, no dependencies. Two routes:
- `GET /crossdomain.xml` → the permissive policy below.
- `POST /api/bacon` → read submitted form data, GET `https://baconipsum.com/api/?type=<type>`, return FDF.

Accept **both** submit encodings and branch on request `Content-Type` (`application/x-www-form-urlencoded` for `cSubmitAs:"HTML"`, `application/vnd.fdf` for the FDF default). FDF parsing is a regex over `/T\s*\(([^)]*)\)\s*/V\s*\(([^)]*)\)`. Accepting both means step 4's two test buttons are conclusive rather than ambiguous.

FDF response — headers matter as much as the body:

```
Content-Type: application/vnd.fdf
Content-Disposition: inline
Cache-Control: no-cache, no-store, max-age=0, must-revalidate
```

```
%FDF-1.2
1 0 obj
<< /FDF << /Fields [ << /T(response) /V(...) >> << /T(status) /V(OK) >> ] >> >>
endobj
trailer
<< /Root 1 0 R >>
%%EOF
```

Escaping is the easy thing to get wrong: in PDF literal strings escape `\` → `\\`, `(` → `\(`, `)` → `\)`, and convert newlines to `\r` (Acrobat's canonical multiline separator; a raw `\r\n` can render a spurious blank line). **The FDF must carry no `/F` or `/UF` key** — Adobe's rules require its absence for the local-file case.

### 3. `web/crossdomain.xml`

`build.sh:142` already does `cp web/* out`, so this lands at the domain root automatically.

```xml
<?xml version="1.0"?>
<cross-domain-policy>
  <site-control permitted-cross-domain-policies="all"/>
  <allow-access-from domain="*"/>
  <allow-http-request-headers-from domain="*" headers="*"/>
</cross-domain-policy>
```

A PDF opened from disk has **no origin**, so every response is cross-domain and needs this; Adobe's alternative for domain-less files is registering a certification-signature fingerprint in the policy file, which drags the whole certificate apparatus back in. Adobe labels `domain="*"` the "least restrictive policy" and advises against it — acceptable here because the origin serves only a static demo page and one public lorem-ipsum relay, with no cookies, auth, or user data. **Revisit if anything authenticated is ever hosted on this domain.**

### 4. `httpdemo.js` — document script

Follows `pdflinux.js` conventions; wired through the existing `create_script` wrapper (`gen_pdf.py:9-13`) which already try/catches into `app.alert`.

```js
function fetch_bacon(submit_as) {
  set_status("Submitting...");
  event.target.doc.submitForm({
    cURL: RELAY_URL + "#FDF",
    cSubmitAs: submit_as,        // "HTML" or "FDF"
    aFields: ["type"],
    bEmpty: true
  });
}
```

Notes: `#FDF` is only *required* when viewing in a browser and harmless standalone. `aFields: ["type"]` keeps the POST minimal while still sending the user-editable parameter. There is no response callback — Acrobat imports the FDF itself, so `status` and `response` simply change value. Have the relay set `status` so a populated field is proof of a completed round trip rather than something to infer.

### 5. `gen_http_pdf.py` — build the demo PDF

Reuses the helpers rather than reimplementing them (all module-level; only the build body sits under `if __name__ == "__main__"`, so the import is side-effect-free):

```python
from gen_pdf import (create_page, create_field, create_button,
                     create_script, create_text, attach_acroform)
```

612×400 page:
- Content-stream text: title, the relay URL, a one-line note that this needs Acrobat.
- `create_field("type", ..., readonly=False)` pre-filled `meat-and-filler` — the live request parameter.
- Two buttons, `/AA /U` → `fetch_bacon("HTML")` and `fetch_bacon("FDF")`, to settle empirically which submit encoding Acrobat pairs with an FDF response.
- `create_field("status", ...)` single-line; `create_field("response", ..., multiline=True)` ~520×210, `Ff = 2|4096`.
- `attach_acroform(writer, fields)` **after** `writer.addpage(page)`.

Build (native Windows venv, pdfrw 0.4 already installed — `build.sh:15`'s `source ./.venv/bin/activate` is a Linux path and doesn't apply, so the demo bypasses `build.sh` entirely):

```
.venv\Scripts\python.exe gen_http_pdf.py out\http_demo.pdf
```

### 6. `functions/api/bacon.js` — production relay

Port of the Python relay to a Pages Function; same FDF body, same headers. `build.sh` needs `cp -r functions out/` (Pages Functions must sit at the deployment root) — verify against `.github/workflows/deploy.yaml`, which publishes `out/`.

## Verification

1. **Relay in isolation**, before Acrobat is involved:
   ```
   curl -i -X POST -d "type=meat-and-filler" http://localhost:8000/api/bacon
   ```
   Expect `Content-Type: application/vnd.fdf`, a `%FDF-1.2` body, balanced parens in `/V(...)`, no `/F` key.
2. **PDF structure**, before opening a GUI:
   ```
   .venv\Scripts\python.exe -c "import pdfrw; r=pdfrw.PdfReader('out/http_demo.pdf'); print(r.Root.AcroForm.NeedAppearances, len(r.Root.AcroForm.Fields))"
   ```
   Expect `true 4`. `AcroForm is None` means the `addpage`/trailer ordering trap was hit.
3. **Fields render on open**, before clicking anything — proves `/DA` + `NeedAppearances`. If they're blank, stop: the HTTP layer isn't the problem yet.
4. **Click Fetch (HTML).** Expect at most one trust prompt — tick "Remember my action for this site". `status` → `OK`, `response` fills with bacon ipsum. Adobe warns that accepting trust via the yellow bar can reload the document and blank the fields; if so, click Fetch again.
5. **Click Fetch (FDF)** and record which encoding worked. Keep the winner, delete the loser.
6. **Confirm it's live:** change `type` to `all-meat`, fetch again, and check the text changes. Fetch twice with the same input and confirm the text differs — the API randomizes, so identical output would mean a cached response, not a real request.
7. **Deploy**, then repeat 4-6 against the Pages URL over https from a PDF opened off local disk — the real target configuration, and the only test that exercises `crossdomain.xml`.
8. **Distribution test:** open the PDF on a machine that has never been configured. One trust prompt, then it works. This is the actual acceptance criterion.

## Risks

- **Which submit encoding pairs with an FDF response is unverified.** Adobe ties the `#FDF` URL suffix to `cSubmitAs` being FDF/XFDF, but also says the suffix is unnecessary standalone. Hence two buttons and a relay that accepts both — a 10-minute experiment instead of a guess.
- **The yellow-bar trust flow may blank fields** on the reload that follows accepting trust. Cosmetic, documented by Adobe, resolved by clicking again.
- **`/JavaScript` in FDF is off the table** — Adobe's own rules conflict on it. Design uses `/Fields` only.
- **Optional 5-minute side-probe:** `Net.SOAP.request` is `Security = No, Product = All` — the one general network API in the reference that isn't privilege-restricted. If it works from document JS on Acrobat 26.1 it would return a real response body to a JS callback and remove the relay entirely. Unverified on modern Acrobat, deprecated since Acrobat 8, SOAP-shaped. Worth one console test out of curiosity; not on the critical path.

## Follow-up (not in this plan)

Fold into `linux.pdf`: feed relay-fetched bytes into the VM via `_console_queue_char` (already exported, `tinyemu/Makefile.pdfjs:29`), or extend the `embedded_files` interceptor at `tinyemu/js/lib.js:70` so the guest-side `/.fscmd` `xhr` command (`tinyemu/fs_net.c:2470` — already compiled and reachable from guest userspace) reaches the real network, giving the Linux guest genuine HTTP.
