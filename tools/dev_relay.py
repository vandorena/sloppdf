#local development relay for http_demo.pdf - stdlib only, no dependencies.
#
#  python tools/dev_relay.py [port]
#
#serves two routes:
#  GET  /crossdomain.xml  - adobe's cross domain policy. a pdf opened from disk
#                           has no origin, so acrobat treats every response as
#                           cross domain and refuses it without this.
#  POST /api/bacon        - reads the submitted form data, fetches baconipsum,
#                           and replies with an fdf that acrobat imports into
#                           the pdf's fields.

import re
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BACON_URL = "https://baconipsum.com/api/?type={}"
ALLOWED_TYPES = ["meat-and-filler", "all-meat"]

CROSSDOMAIN = b"""<?xml version="1.0"?>
<cross-domain-policy>
  <site-control permitted-cross-domain-policies="all"/>
  <allow-access-from domain="*"/>
  <allow-http-request-headers-from domain="*" headers="*"/>
</cross-domain-policy>
"""

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

#note there is deliberately no /F or /UF key here - adobe's enhanced security
#rules require its absence for a pdf that was opened from the local filesystem.
def build_fdf(values):
  fields = "".join(
    f"<< /T({pdf_escape(name)}) /V({pdf_escape(value)}) >>\n"
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

def fetch_bacon(bacon_type):
  if bacon_type not in ALLOWED_TYPES:
    bacon_type = ALLOWED_TYPES[0]
  url = BACON_URL.format(urllib.parse.quote(bacon_type))
  with urllib.request.urlopen(url, timeout=15) as resp:
    raw = resp.read().decode("utf-8")
  #the api returns a json array of paragraph strings. parsing it with a regex
  #keeps this file dependency free and the payload shape is fixed.
  paragraphs = re.findall(r'"((?:[^"\\]|\\.)*)"', raw)
  return "\r\r".join(p.replace('\\"', '"') for p in paragraphs)

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
    if self.path.split("?")[0] == "/crossdomain.xml":
      self._send(200, CROSSDOMAIN, "text/x-cross-domain-policy")
    else:
      self._send(404, b"not found\n", "text/plain")

  def do_POST(self):
    if self.path.split("#")[0].split("?")[0] != "/api/bacon":
      self._send(404, b"not found\n", "text/plain")
      return

    length = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(length)
    content_type = (self.headers.get("Content-Type") or "").lower()
    submitted = parse_submission(body, content_type)
    bacon_type = submitted.get("type", ALLOWED_TYPES[0])

    print(f"  submitted as {content_type!r} -> {submitted!r}")

    try:
      text = fetch_bacon(bacon_type)
      values = {
        "response": text,
        "status": f"OK - {len(text)} chars, type={bacon_type}",
      }
    except Exception as e:
      values = {"response": "", "status": f"relay error: {e}"}

    self._send(200, build_fdf(values), "application/vnd.fdf")

if __name__ == "__main__":
  port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
  print(f"relay on http://localhost:{port}  (POST /api/bacon)")
  ThreadingHTTPServer(("", port), Handler).serve_forever()
