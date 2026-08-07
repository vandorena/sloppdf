#builds the pdf that makes a live http request via doc.submitForm().
#
#  .venv\Scripts\python.exe gen_http_pdf.py public/http_demo.pdf        (deployed)
#  .venv\Scripts\python.exe gen_http_pdf.py out/http_demo.pdf local     (dev relay)

import sys
import pathlib

from pdfrw import PdfWriter
from pdfrw.objects.pdfname import PdfName
from pdfrw.objects.pdfdict import PdfDict
from pdfrw.objects.pdfarray import PdfArray

from pdfform import (create_page, create_field, create_button, create_script,
                     create_text, attach_acroform)

#the deployed relay. the pdf has to be built against wherever it will actually
#run: a pdf opened from disk has no origin of its own, so acrobat treats every
#reply as cross domain and will only accept one from a host that serves a
#permissive crossdomain.xml (see public/crossdomain.xml).
DEFAULT_RELAY = "https://slop.alexvd.dev/api/story"

#for local testing, pass the dev relay explicitly:
#  gen_http_pdf.py out/http_demo.pdf http://127.0.0.1:8000/api/story
#use 127.0.0.1 rather than localhost - "localhost" resolves to ::1 first on
#windows, and with the dev relay bound to ipv4 only that costs a ~2 second
#fallback on every single request. the literal address answers in ~3ms.
LOCAL_RELAY = "http://127.0.0.1:8000/api/story"

def create_action_button(name, x, y, width, height, caption, js):
  button = create_button(name, x, y, width, height, caption)
  button.AA = PdfDict()
  button.AA.U = create_script(js)
  return button

def create_submit_button(name, x, y, width, height, caption, submit_as):
  return create_action_button(name, x, y, width, height, caption,
                              f"generate_story('{submit_as}')")

if __name__ == "__main__":
  out_path = sys.argv[1]
  relay_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_RELAY
  #shorthand so testing against the dev relay does not mean retyping the url
  if relay_url == "local":
    relay_url = LOCAL_RELAY

  js = pathlib.Path("httpdemo.js").read_text()
  js = js.replace("__relay_url__", relay_url)

  width = 612
  height = 560

  writer = PdfWriter()
  page = create_page(width, height)
  page.AA = PdfDict()
  page.AA.O = create_script(js)

  fields = [
    #every field here is left writable on purpose. read only would be tidier, but
    #these are the fields the relay's fdf reply writes into, and it removes any
    #chance the flag interferes with the import. it also lets you select and copy
    #the story out. nothing may be marked Required - see FF_REQUIRED in pdfform.py.
    create_field("topic", 100, 500, 200, 16, "", readonly=False),
    create_field("status", 100, 470, 480, 16, "Loading...", readonly=False),
    #carries the relay's job id between polls. kept visible because when this
    #breaks, whether the id survived the fdf import is the first thing to check.
    create_field("job", 100, 450, 120, 12, "", readonly=False),
    #a 300 word story is roughly 1800 characters, so this needs the room
    create_field("response", 40, 60, 532, 370, "", multiline=True, readonly=False),
    create_submit_button("gen_html", 330, 498, 110, 20, "Generate (HTML)", "HTML"),
    create_submit_button("gen_fdf", 450, 498, 110, 20, "Generate (FDF)", "FDF"),
    #manual fallback in case app.setTimeOut does not fire in this context
    create_action_button("poll_now", 230, 448, 70, 16, "Poll now", "poll_manually()"),
  ]

  page.Contents = PdfDict()
  page.Contents.stream = "\n".join([
    create_text(40, 528, 16, "sloppdf - a story, written live, from inside a PDF"),
    create_text(40, 504, 8, "topic:"),
    create_text(305, 504, 6, "(optional)"),
    create_text(40, 474, 8, "status:"),
    create_text(40, 454, 8, "job:"),
    create_text(230, 454, 6, "(relay job id - polling handle)"),
    create_text(40, 436, 8, "story:"),
    create_text(40, 40, 7, f"Relay: {relay_url}"),
    create_text(40, 30, 7, "Requires Adobe Acrobat or Reader. Will not work in a browser's PDF viewer."),
    create_text(40, 20, 7, "Acrobat may ask once whether to allow this document to contact the relay host - choose Allow."),
  ])

  page.Annots = PdfArray(fields)
  writer.addpage(page)
  #must come after addpage() - see the note on attach_acroform
  attach_acroform(writer, fields)
  writer.write(out_path)

  print(f"wrote {out_path} (relay: {relay_url})")
