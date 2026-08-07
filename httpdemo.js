//document level javascript for slop.pdf
//this makes a live http request from inside the pdf using doc.submitForm(),
//which - unlike Net.HTTP.request - is not security restricted and works in the
//free reader outside of a browser. the relay posts to /api/story, which asks the
//model for a story and replies with an fdf that acrobat imports into these
//fields. the api key lives on the relay - never in this document, which anyone
//who opens the pdf can read in full.
//
//generation takes seconds, and acrobat will not hold a submitForm
//connection open that long - it drops the socket and the reply is lost. so the
//relay answers immediately with a job id, and this script re-submits with that
//id until the job reports done. each individual request is fast.

var RELAY_URL = "__relay_url__";

//the default model takes 13-20s, but a reasoning model swapped in via
//HACKCLUB_AI_MODEL can take three minutes. the limit covers that slow tail rather
//than abandoning jobs that were about to succeed.
var POLL_INTERVAL = 4000; //ms between polls
var POLL_LIMIT = 75;      //75 * 4s = 5 minutes

//at document level `this` is the Doc, so capture it here. note that upstream
//linuxpdf reaches fields via globalThis.getField(), which works in pdfium but
//not in acrobat, where getField is a Doc method rather than a global.
var DOC = this;

//"HTML" (urlencoded) or "FDF" (acrobat's default). both are wired up so we can
//find out which one acrobat actually pairs with an fdf response - adobe's docs
//are inconsistent on this.
var submit_mode = "HTML";

function set_status(msg) {
  DOC.getField("status").value = msg;
}

function get_status() {
  return "" + DOC.getField("status").value;
}

//the relay writes "OK - ..." when the story has arrived and "relay error: ..."
//when it failed. anything else - including our own placeholder text before the
//first fdf lands - means keep waiting.
function is_finished(status) {
  return status.indexOf("OK -") === 0 || status.indexOf("relay error") === 0;
}

function submit_now() {
  DOC.submitForm({
    cURL: RELAY_URL + "#FDF",
    cSubmitAs: submit_mode,
    aFields: ["topic", "job"],
    bEmpty: true
  });
}

function generate_story(mode) {
  submit_mode = mode;
  //an empty job field tells the relay this is a fresh request rather than a poll
  DOC.getField("job").value = "";
  DOC.getField("response").value = "";
  set_status("Starting (" + mode + ")...");

  submit_now();

  //schedule the first poll unconditionally. do NOT inspect the status here - the
  //fdf reply has not been imported yet at this point, so any check on it reads
  //stale text. gating the first poll on seeing "WORKING" meant polling never
  //started at all, and every click just leaked a new job on the relay.
  schedule_poll(1);
}

function schedule_poll(attempt) {
  //app.setTimeOut takes a string of javascript, not a function
  app.setTimeOut("try {poll_once(" + attempt + ")} catch (e) {app.alert(e)}",
                 POLL_INTERVAL);
}

function poll_once(attempt) {
  if (is_finished(get_status()))
    return;

  if (attempt > POLL_LIMIT) {
    set_status("Gave up after " + attempt + " polls. Is the relay still running?");
    return;
  }

  submit_now();
  schedule_poll(attempt + 1);
}

//manual fallback. if app.setTimeOut turns out not to fire in this context, this
//button still retrieves the result - and it is the quickest way to tell the two
//failure modes apart.
function poll_manually() {
  if (!DOC.getField("job").value) {
    set_status("No job to poll - click Generate first.");
    return;
  }
  submit_now();
}

set_status("Ready - topic is optional. Click Generate.");
