# sloppdf

A PDF file that gets a clanker (in this case the LLM Qwen) to give you a unique all new 2026 Ford F1-Story.

<img width="769" height="686" alt="image" src="https://github.com/user-attachments/assets/e67d45d5-0ca6-453d-9e51-64ed7f4c2412" />


** Btw you gotta open this in Adobe Acrobat or Acrobat Reader.** It won't work in Chrome,
Firefox, Edge, or Preview --- they care about security 😿. In Adobe Acrobat you may also get a security warning,
if you do, ensure that the domain matches my domain, https://slop.alexvd.dev, and you should
be safe.

## How it works

PDF files can contain JavaScript, and Acrobat gives that JavaScript exactly one
way to reach the network: `Doc.submitForm()`, which posts the form's fields to a
URL. The trick is the reply — if the server answers with an
`application/vnd.fdf` document, Acrobat parses it and writes the values straight
back into the form's fields. That is the return channel.

```
http_demo.pdf                              relay (holds the api key)
  [topic: ....... (optional)]
  [Generate] ──POST─────────▶ /api/story ──┬─ starts job, replies in ms
             ◀── job id + WORKING ─────────┘        │
  ...every 4s:                                      ├──▶ ai.hackclub.com
  [poll]     ──POST job=xxx ─▶              ────────┘         │
             ◀── WORKING, or the story ◀───── job done ◀───────┘
```

The relay exists for two unavoidable reasons. Anyone who opens a PDF can read
every byte of it, so an API key stored inside the document is a published key.
And Acrobat drops a `submitForm` connection long before a generation finishes,
so the relay answers immediately with a job number and lets the document poll.

`Net.HTTP.request` would be the obvious API to reach for, but it can only be
called from a folder-level script installed into Acrobat — which no distributed
PDF can rely on.

## Layout

| Path | |
|------|---|
| `gen_http_pdf.py` | builds the PDF |
| `pdfform.py` | AcroForm widget helpers |
| `httpdemo.js` | the document-level JavaScript |
| `tools/dev_relay.py` | local relay, stdlib only |
| `api/story.js` | the deployed relay (Vercel) |
| `public/` | landing page, policy file, built PDF |

## Running it locally

```sh
python3 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

cp .env.example .env      # add your HACKCLUB_AI_KEY

.venv/Scripts/python.exe tools/dev_relay.py 8000
.venv/Scripts/python.exe gen_http_pdf.py out/http_demo.pdf local
```

Then open `out/http_demo.pdf` in Acrobat. Full detail, including the deployment
setup and a list of the things that silently break this, is in
[docs/http-demo.md](docs/http-demo.md).

## Credits

This is a fork of [linuxpdf](https://github.com/ading2210/linuxpdf) by
[@ading2210](https://github.com/ading2210/), which runs an entire RISC-V Linux
emulator inside a PDF using the same PDF-JavaScript tricks. `pdfform.py` is
derived from that project's `gen_pdf.py`.

See also [DoomPDF](https://github.com/ading2210/doompdf), and
[TinyEMU](https://bellard.org/tinyemu/) by Fabrice Bellard, which linuxpdf's
emulator is based on.

## License

GNU GPL v3, inherited from linuxpdf. See [LICENSE](LICENSE).

```
sloppdf - a PDF that writes stories
Copyright (C) 2026 vandorena

Derived from ading2210/linuxpdf, Copyright (C) 2025 ading2210

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.
```
