//document level javascript for http_demo.pdf
//this makes a live http request from inside the pdf using doc.submitForm(),
//which - unlike Net.HTTP.request - is not security restricted and works in the
//free reader outside of a browser. the relay posts to /api/bacon, which fetches
//baconipsum and replies with an fdf that acrobat imports back into these fields.

var RELAY_URL = "__relay_url__";

//at document level `this` is the Doc, so capture it here. note that pdflinux.js
//reaches fields via globalThis.getField() - that works in pdfium but not in
//acrobat, where getField is a Doc method rather than a global.
var DOC = this;

function set_status(msg) {
  DOC.getField("status").value = msg;
}

//submit_as is either "HTML" (urlencoded, easy to parse server side) or "FDF"
//(acrobat's default). both are wired up so we can find out which one acrobat
//actually pairs with an fdf response - adobe's docs are inconsistent on this.
function fetch_bacon(submit_as) {
  set_status("Submitting (" + submit_as + ")...");
  DOC.getField("response").value = "";

  //there is no response callback here. acrobat parses the fdf reply itself and
  //writes the values straight into the fields, so the relay sets "status" too -
  //seeing it change is what proves the round trip completed.
  DOC.submitForm({
    cURL: RELAY_URL + "#FDF",
    cSubmitAs: submit_as,
    aFields: ["type"],
    bEmpty: true
  });
}

set_status("Ready - click a Fetch button.");
