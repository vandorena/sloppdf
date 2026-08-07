#builds a standalone pdf that makes a live http request via doc.submitForm().
#unlike gen_pdf.py this needs no emscripten build - just pdfrw - so it runs on
#windows directly:
#  .venv\Scripts\python.exe gen_http_pdf.py out\http_demo.pdf

import sys
import pathlib

from pdfrw import PdfWriter
from pdfrw.objects.pdfname import PdfName
from pdfrw.objects.pdfdict import PdfDict
from pdfrw.objects.pdfarray import PdfArray

from gen_pdf import (create_page, create_field, create_button, create_script,
                     create_text, attach_acroform)

DEFAULT_RELAY = "http://localhost:8000/api/bacon"

def create_submit_button(name, x, y, width, height, caption, submit_as):
  button = create_button(name, x, y, width, height, caption)
  button.AA = PdfDict()
  button.AA.U = create_script(f"fetch_bacon('{submit_as}')")
  return button

if __name__ == "__main__":
  out_path = sys.argv[1]
  relay_url = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_RELAY

  js = pathlib.Path("httpdemo.js").read_text()
  js = js.replace("__relay_url__", relay_url)

  width = 612
  height = 400

  writer = PdfWriter()
  page = create_page(width, height)
  page.AA = PdfDict()
  page.AA.O = create_script(js)

  fields = [
    #the live request parameter - editable, so this must not be read only
    create_field("type", 100, 340, 200, 16, "meat-and-filler", readonly=False),
    create_field("status", 100, 310, 480, 16, "Loading..."),
    create_field("response", 40, 60, 532, 230, "", multiline=True),
    create_submit_button("fetch_html", 330, 338, 110, 20, "Fetch (HTML)", "HTML"),
    create_submit_button("fetch_fdf", 450, 338, 110, 20, "Fetch (FDF)", "FDF"),
  ]

  page.Contents = PdfDict()
  page.Contents.stream = "\n".join([
    create_text(40, 368, 16, "sloppdf - live HTTP request from inside a PDF"),
    create_text(40, 344, 8, "type:"),
    create_text(40, 314, 8, "status:"),
    create_text(40, 296, 8, "response:"),
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
