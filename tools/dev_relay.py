#local development relay for http_demo.pdf - stdlib only, no dependencies.
#
#  cp .env.example .env   and put your key in it
#  python tools/dev_relay.py [port]
#
#serves four routes:
#  GET  /                 - a static landing page: download link and explanation.
#  GET  /http_demo.pdf    - the built pdf, read off disk on each request.
#  GET  /crossdomain.xml  - adobe's cross domain policy. a pdf opened from disk
#                           has no origin, so acrobat treats every response as
#                           cross domain and refuses it without this.
#  POST /api/story        - reads the submitted form data, asks the model for a
#                           story, and replies with an fdf that acrobat imports
#                           into the pdf's fields.
#
#the api key lives here and never goes near the pdf. anyone who opens a pdf can
#read every byte of it, so a key embedded in the document is a published key.

import json
import os
import pathlib
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#stdout is a pipe when this runs in the background, and python buffers that -
#without flushing, none of the diagnostics below ever appear.
def log(msg):
  print(msg, flush=True)

#minimal .env reader so the key does not have to be exported by hand every time.
#a real environment variable wins over the file, which keeps CI and one off
#overrides working.
def load_dotenv(path):
  if not path.is_file():
    return
  for line in path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip().strip('"').strip("'")
    os.environ.setdefault(key, value)

ENV_PATH = pathlib.Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

AI_URL = os.environ.get("HACKCLUB_AI_URL", "https://ai.hackclub.com/proxy/v1/chat/completions")
#the leading ~ is part of the model id on this proxy - it marks a floating alias.
#without it the proxy returns "is not a valid model ID".
AI_MODEL = os.environ.get("HACKCLUB_AI_MODEL", "~deepseek/deepseek-v4-flash-latest")
AI_KEY = os.environ.get("HACKCLUB_AI_KEY", "")

#keep in sync with functions/api/story.js
BASE_PROMPT = (
  "Think of something you haven't thought of before. Try your best to be random. "
  "Try to decide if your text is like the number 7 or not. Then decide a story. "
  "Something obscene. Under 300 words. Make it weird and goofy, but not "
  "offputting. It should feel sloppy, but not too sloppy."
)

MAX_TOPIC_LEN = 200

#this has to cover reasoning tokens as well as the story. the default model burns
#300-900 tokens thinking before it writes anything, and if the budget runs out
#first the api returns finish_reason=length with content=null - no story at all.
MAX_TOKENS = int(os.environ.get("HACKCLUB_AI_MAX_TOKENS", "3000"))

CROSSDOMAIN = b"""<?xml version="1.0"?>
<cross-domain-policy>
  <site-control permitted-cross-domain-policies="all"/>
  <allow-access-from domain="*"/>
  <allow-http-request-headers-from domain="*" headers="*"/>
</cross-domain-policy>
"""

#a static landing page - a download link and an explanation, no javascript. the
#pdf itself is the interactive part; this is just how you get it.
INDEX = b"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>sloppdf - a PDF that writes stories</title>
<style>
  body { font: 16px/1.6 Georgia, serif; max-width: 40rem; margin: 0 auto;
         padding: 3rem 1.25rem 5rem; background: #fbfaf7; color: #23231f; }
  h1 { font-size: 1.6rem; margin-bottom: .25rem; }
  .sub { color: #6b6b63; font-style: italic; margin-top: 0; }
  h2 { font-size: 1.05rem; margin-top: 2.25rem; }
  a.dl { display: inline-block; margin: 1.5rem 0; padding: .7rem 1.3rem;
         background: #23231f; color: #fbfaf7; text-decoration: none;
         border-radius: 4px; font-family: system-ui, sans-serif;
         font-size: .95rem; }
  code { background: #efeee9; padding: .1em .35em; border-radius: 3px;
         font-size: .85em; }
  .warn { border-left: 3px solid #c2410c; padding: .5rem 0 .5rem .9rem;
          background: #fdf4ee; }
  ol { padding-left: 1.3rem; }
  li { margin: .4rem 0; }
  footer { margin-top: 3rem; color: #6b6b63; font-size: .85rem; }
</style>

<h1>sloppdf</h1>
<p class="sub">A PDF file that asks a language model for a story, over the
internet, while you are reading it.</p>

<a class="dl" href="/http_demo.pdf">Download the PDF</a>

<p class="warn"><strong>Open it in Adobe Acrobat or Acrobat Reader.</strong>
It will not work in Chrome, Firefox, Edge, or Preview &mdash; their PDF viewers
implement no networking at all.</p>

<h2>How it works</h2>

<p>PDF files can contain JavaScript. Acrobat gives that JavaScript exactly one way
to reach the network: <code>Doc.submitForm()</code>, which posts the form's fields
to a URL. The trick is the reply &mdash; if the server answers with an
<code>application/vnd.fdf</code> document, Acrobat parses it and writes the values
straight back into the form's fields. That is the return channel.</p>

<ol>
  <li>You type an optional topic and click <em>Generate</em>.</li>
  <li>The PDF posts that topic to a small relay server.</li>
  <li>The relay asks the model for a story, and immediately replies with a job
      number &mdash; not the story.</li>
  <li>The PDF re-submits every few seconds asking whether that job is done.</li>
  <li>When it is, the relay sends the story back as FDF, and the text appears in
      the page you are looking at.</li>
</ol>

<h2>Why the relay exists</h2>

<p>Two reasons, both unavoidable.</p>

<p><strong>The API key.</strong> Anyone who opens a PDF can read every byte of it,
so a key stored inside the document is a published key. The relay holds it
instead.</p>

<p><strong>Acrobat will not wait.</strong> A story takes anywhere from 15 seconds
to three minutes to write. Acrobat drops a <code>submitForm</code> connection
long before that, and the reply is simply lost. So the relay never makes the PDF
wait: it hands back a job number in milliseconds and lets the document poll.</p>

<h2>Caveats</h2>

<p>Acrobat will ask once whether to allow the document to contact the relay.
That is its cross-domain check &mdash; a PDF opened from your disk has no origin
of its own, so every response counts as cross-domain. Choose <em>Allow</em>.</p>

<p>The window freezes for a moment on each poll, because <code>submitForm</code>
blocks the interface. The status line counts the seconds so you can tell it is
still working.</p>

<footer>Built on <a href="https://github.com/ading2210/linuxpdf">linuxpdf</a> by
ading2210, which runs an entire RISC-V Linux emulator in a PDF the same way.</footer>
"""

#serve the built pdf straight off disk so a rebuild is picked up without a restart
PDF_PATH = pathlib.Path(__file__).resolve().parent.parent / "out" / "http_demo.pdf"

#escape a python string so it is a valid pdf literal string. the backslash must
#be replaced first, otherwise it doubles the escapes added afterwards.
def pdf_escape(text):
  text = text.replace("\\", "\\\\")
  text = text.replace("(", "\\(").replace(")", "\\)")
  #acrobat's line separator inside a multiline field is a carriage return, and a
  #literal \r\n can render an extra blank line. emit the two character escape
  #sequence rather than a raw CR byte - the spec reads a real end of line inside
  #a literal string as \n, which would silently change the separator.
  text = text.replace("\r\n", "\n").replace("\r", "\n")
  text = text.replace("\n", "\\r")
  return text

#a pdf literal string can only carry the font's 8 bit encoding, and model output
#is full of em dashes and smart quotes. fall back to a utf-16be hex string (the
#leading BOM is what tells acrobat it is unicode) when the text is not ascii.
#ascii still takes the literal path so the fdf stays readable while debugging.
def pdf_text_string(text):
  text = text.replace("\r\n", "\n").replace("\r", "\n")
  if text.isascii():
    return "(" + pdf_escape(text) + ")"
  encoded = ("﻿" + text.replace("\n", "\r")).encode("utf-16-be")
  return "<" + encoded.hex() + ">"

#note there is deliberately no /F or /UF key here - adobe's enhanced security
#rules require its absence for a pdf that was opened from the local filesystem.
def build_fdf(values):
  fields = "".join(
    f"<< /T{pdf_text_string(name)} /V{pdf_text_string(value)} >>\n"
    for name, value in values.items()
  )
  return f"""%FDF-1.2
1 0 obj
<< /FDF << /Fields [
{fields}] >> >>
endobj
trailer
<< /Root 1 0 R >>
%%EOF
""".encode("utf-8")

#acrobat posts fdf by default, or urlencoded when cSubmitAs is "HTML". accept
#both so the pdf's two test buttons are conclusive.
def parse_submission(body, content_type):
  if "fdf" in content_type:
    text = body.decode("utf-8", "replace")
    return dict(re.findall(r"/T\s*\(([^)]*)\)\s*/V\s*\(([^)]*)\)", text))
  parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
  return {key: vals[0] for key, vals in parsed.items()}

def build_prompt(topic):
  prompt = BASE_PROMPT
  if topic:
    prompt += f"\n\nWork this topic in somewhere: {topic}"
  #the model is asked to be random, so give it something to be random from -
  #this also stops any layer in between serving a cached completion.
  prompt += f"\n\n(entropy: {secrets.token_hex(4)} - ignore this token, it only exists to vary your output)"
  return prompt

def generate_story(topic):
  if not AI_KEY:
    raise RuntimeError("HACKCLUB_AI_KEY is not set")

  payload = json.dumps({
    "model": AI_MODEL,
    "messages": [{"role": "user", "content": build_prompt(topic)}],
    "temperature": 1.0,
    "max_tokens": MAX_TOKENS,
  }).encode("utf-8")

  request = urllib.request.Request(AI_URL, data=payload, method="POST", headers={
    "Authorization": "Bearer " + AI_KEY,
    "Content-Type": "application/json",
  })

  try:
    #generous: a reasoning model can spend minutes before it emits anything, and
    #cutting it off at 90s killed generations that would have succeeded
    with urllib.request.urlopen(request, timeout=300) as resp:
      body = json.loads(resp.read().decode("utf-8"))
  except urllib.error.HTTPError as e:
    detail = e.read().decode("utf-8", "replace")[:200]
    raise RuntimeError(f"upstream {e.code}: {detail}") from None

  choice = body["choices"][0]
  content = (choice["message"].get("content") or "").strip()

  #a reasoning model that exhausts max_tokens while still thinking returns
  #finish_reason=length with a null content, which is otherwise a baffling
  #failure - name it so the pdf's status field says something useful.
  if not content:
    reason = choice.get("finish_reason")
    used = body.get("usage", {}).get("completion_tokens_details", {}).get("reasoning_tokens")
    if reason == "length":
      raise RuntimeError(f"model used all {MAX_TOKENS} tokens on reasoning "
                         f"({used}) and never wrote the story - raise "
                         f"HACKCLUB_AI_MAX_TOKENS")
    raise RuntimeError(f"model returned no content (finish_reason={reason})")

  return content

#acrobat will not wait 15-35 seconds for a submitForm response - it drops the
#connection first, and the reply is lost (WinError 10054 on our side). so the
#request handler never calls the model directly. it starts a background job and
#answers immediately; the pdf then re-submits with the job id until it is done.
JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 600

#an impatient double-click used to start a second job and orphan the first, which
#burns api credits and throws away a story that was already paid for. reuse a
#recent running job for the same topic instead.
DEDUPE_WINDOW = 15

def find_running_job(topic):
  now = time.time()
  with JOBS_LOCK:
    for job_id, job in JOBS.items():
      if (job["state"] == "running" and job.get("topic") == topic
          and now - job["started"] < DEDUPE_WINDOW):
        return job_id
  return None

def start_job(topic):
  existing = find_running_job(topic)
  if existing:
    log(f"  reusing running job {existing} (topic={topic!r})")
    return existing

  job_id = secrets.token_hex(6)
  with JOBS_LOCK:
    JOBS[job_id] = {"state": "running", "started": time.time(), "story": "",
                    "error": "", "topic": topic}

  def run():
    try:
      story = generate_story(topic)
      with JOBS_LOCK:
        JOBS[job_id].update(state="done", story=story)
      log(f"  job {job_id}: done, {len(story)} chars")
    except Exception as e:
      with JOBS_LOCK:
        JOBS[job_id].update(state="error", error=str(e))
      log(f"  job {job_id}: ERROR {e}")

  threading.Thread(target=run, daemon=True).start()
  log(f"  job {job_id}: started (topic={topic!r})")
  return job_id

def reap_jobs():
  now = time.time()
  with JOBS_LOCK:
    for key in [k for k, v in JOBS.items() if now - v["started"] > JOB_TTL]:
      del JOBS[key]

#the pdf's document script polls on the "WORKING" prefix, so these strings are
#part of the contract - see poll_result() in httpdemo.js.
def job_fields(job_id):
  with JOBS_LOCK:
    job = JOBS.get(job_id)
    if job is None:
      return {"response": "", "status": "relay error: unknown or expired job", "job": ""}
    state, story, error = job["state"], job["story"], job["error"]
    waited = int(time.time() - job["started"])

  if state == "running":
    return {"status": f"WORKING - generating, {waited}s elapsed...", "job": job_id}
  if state == "error":
    return {"response": "", "status": f"relay error: {error}", "job": ""}
  return {"response": story, "status": f"OK - {len(story)} chars in {waited}s", "job": ""}

class Handler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"

  def _send(self, status, body, content_type):
    self.send_response(status)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Content-Disposition", "inline")
    self.send_header("Cache-Control", "no-cache, no-store, max-age=0, must-revalidate")
    self.send_header("Pragma", "no-cache")
    self.end_headers()
    self.wfile.write(body)

  def do_GET(self):
    path = self.path.split("?")[0]
    if path == "/crossdomain.xml":
      self._send(200, CROSSDOMAIN, "text/x-cross-domain-policy")
    elif path in ("/", "/index.html"):
      self._send(200, INDEX, "text/html; charset=utf-8")
    elif path == "/http_demo.pdf":
      if PDF_PATH.is_file():
        self._send(200, PDF_PATH.read_bytes(), "application/pdf")
      else:
        self._send(404, b"http_demo.pdf has not been built yet - run "
                        b"gen_http_pdf.py\n", "text/plain")
    else:
      self._send(404, b"not found\n", "text/plain")

  def do_POST(self):
    if self.path.split("#")[0].split("?")[0] != "/api/story":
      self._send(404, b"not found\n", "text/plain")
      return

    length = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(length)
    content_type = (self.headers.get("Content-Type") or "").lower()
    submitted = parse_submission(body, content_type)
    topic = submitted.get("topic", "").strip()[:MAX_TOPIC_LEN]
    job_id = submitted.get("job", "").strip()

    log(f"[{time.strftime('%H:%M:%S')}] POST content-type={content_type!r} "
        f"len={length} topic={topic!r} job={job_id!r}")
    log(f"  raw body: {body[:300]!r}")

    reap_jobs()
    #an empty job means "this is a fresh request"; otherwise the pdf is polling
    values = job_fields(job_id if job_id else start_job(topic))

    payload = build_fdf(values)
    log(f"  replying {len(payload)} bytes, status={values['status']!r}")
    try:
      self._send(200, payload, "application/vnd.fdf")
    except ConnectionError as e:
      #acrobat closed the socket before reading the reply
      log(f"  !! client dropped the connection before reading: {e}")

if __name__ == "__main__":
  port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
  if not AI_KEY:
    log("WARNING: HACKCLUB_AI_KEY is not set - requests will return an error")
  log(f"relay on http://localhost:{port}  (POST /api/story, model {AI_MODEL})")
  ThreadingHTTPServer(("", port), Handler).serve_forever()
