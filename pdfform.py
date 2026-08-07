#helpers for building acroform widgets with pdfrw.
#
#derived from gen_pdf.py in ading2210/linuxpdf, Copyright (C) 2025 ading2210,
#and distributed under the same licence - the GNU GPL v3. See LICENSE.
#
#changes from the original: named /Ff flag constants (the original set Ff = 2 for
#every field, which is Required rather than ReadOnly), a /DA on each widget, an
#attach_acroform() that writes the catalog's /AcroForm - without which acrobat
#cannot resolve getField() at all - and indirect field objects so pdfrw does not
#duplicate a widget that appears in both /Annots and /Fields.

from pdfrw import PdfWriter
from pdfrw.objects.pdfname import PdfName
from pdfrw.objects.pdfstring import PdfString
from pdfrw.objects.pdfdict import PdfDict, IndirectPdfDict
from pdfrw.objects.pdfarray import PdfArray
from pdfrw.objects.pdfobject import PdfObject

#field flags, from the /Ff bit positions in the pdf spec. these are 1-based bit
#positions, so bit 1 is value 1 and bit 2 is value 2 - it is very easy to write 2
#for ReadOnly and actually get Required, which makes acrobat refuse to submit the
#form with "At least one required field was empty".
FF_READONLY = 1     #bit 1
FF_REQUIRED = 2     #bit 2 - deliberately never set here
FF_MULTILINE = 4096 #bit 13
FF_PUSHBUTTON = 65536 #bit 17

#the default appearance string used for form fields - /Helv must exist in the
#acroform's /DR or acrobat has no font to draw the field's value with
DEFAULT_DA = "/Helv 9 Tf 0 g"

def create_script(js):
  action = PdfDict()
  action.S = PdfName.JavaScript
  action.JS = "try {"+js+"} catch (e) {app.alert(e.stack || e)}"
  return action
  
def create_page(width, height):
  page = PdfDict()
  page.Type = PdfName.Page
  page.MediaBox = PdfArray([0, 0, width, height])

  page.Resources = PdfDict()
  page.Resources.Font = PdfDict()
  page.Resources.Font.F1 = PdfDict()
  page.Resources.Font.F1.Type = PdfName.Font
  page.Resources.Font.F1.Subtype = PdfName.Type1
  page.Resources.Font.F1.BaseFont = PdfName.Courier
  
  return page

def create_field(name, x, y, width, height, value="", f_type=PdfName.Tx,
                 multiline=False, readonly=True, da=DEFAULT_DA):
  flags = 0
  if readonly:
    flags |= FF_READONLY
  if multiline:
    flags |= FF_MULTILINE

  annotation = PdfDict()
  annotation.Type = PdfName.Annot
  annotation.Subtype = PdfName.Widget
  annotation.FT = f_type
  annotation.Ff = flags
  annotation.Rect = PdfArray([x, y, x + width, y + height])
  annotation.T = PdfString.encode(name)
  annotation.V = PdfString.encode(value)
  annotation.DA = PdfString.encode(da)
  annotation.F = 4 #print

  annotation.BS = PdfDict()
  annotation.BS.W = 0

  #the field must be an indirect object, otherwise pdfrw duplicates it when it
  #appears in both the page's /Annots and the acroform's /Fields
  annotation.indirect = True

  return annotation

def create_text(x, y, size, txt):
  return f"""
  BT
  /F1 {size} Tf
  {x} {y} Td ({txt}) Tj
  ET
  """

def create_button(name, x, y, width, height, value):
  button = create_field(name, x, y, width, height, f_type=PdfName.Btn)
  button.AA = PdfDict()
  button.Ff = FF_PUSHBUTTON
  button.MK = PdfDict()
  button.MK.BG = PdfArray([0.90])
  button.MK.CA = value
  return button

#acrobat resolves getField() names through the catalog's /AcroForm /Fields, not
#through the page's /Annots like pdfium does, so without this the fields are
#invisible to javascript in acrobat.
#must be called *after* writer.addpage() - addpage() resets writer._trailer, so
#anything set on the catalog before it is silently discarded.
def attach_acroform(writer, fields):
  font = IndirectPdfDict()
  font.Type = PdfName.Font
  font.Subtype = PdfName.Type1
  font.BaseFont = PdfName.Helvetica
  font.Encoding = PdfName.WinAnsiEncoding

  acroform = PdfDict()
  acroform.Fields = PdfArray(fields)
  acroform.DA = PdfString.encode(DEFAULT_DA)
  acroform.DR = PdfDict()
  acroform.DR.Font = PdfDict()
  acroform.DR.Font.Helv = font
  #pdfrw has no boolean type - a bare True would serialise as "True"
  acroform.NeedAppearances = PdfObject("true")

  writer.trailer.Root.AcroForm = acroform
