//cloudflare pages function - the production counterpart of tools/dev_relay.py.
//
//the pdf cannot call baconipsum directly: the only network api acrobat exposes
//to a plain document is doc.submitForm(), whose response must come back as fdf
//for acrobat to import it into form fields. so this endpoint takes the pdf's
//form post, performs the real http request, and re-wraps the result as fdf.

const BACON_URL = "https://baconipsum.com/api/?type=";
const ALLOWED_TYPES = ["meat-and-filler", "all-meat"];

//escape a string so it is a valid pdf literal string. the backslash must go
//first or it doubles the escapes added after it.
function pdfEscape(text) {
  return text
    .replace(/\\/g, "\\\\")
    .replace(/\(/g, "\\(")
    .replace(/\)/g, "\\)")
    //acrobat's separator in a multiline field is a carriage return. emit the two
    //character escape rather than a raw CR byte - the spec reads a real end of
    //line inside a literal string as \n, silently changing the separator.
    .replace(/\r\n|\r|\n/g, "\\r");
}

//no /F or /UF key here - adobe's enhanced security rules require its absence
//for a pdf that was opened from the local filesystem.
function buildFdf(values) {
  const fields = Object.entries(values)
    .map(([name, value]) => `<< /T(${pdfEscape(name)}) /V(${pdfEscape(value)}) >>`)
    .join("\n");
  return `%FDF-1.2
1 0 obj
<< /FDF << /Fields [
${fields}
] >> >>
endobj
trailer
<< /Root 1 0 R >>
%%EOF
`;
}

//acrobat posts fdf by default, or urlencoded when cSubmitAs is "HTML".
async function parseSubmission(request) {
  const contentType = (request.headers.get("content-type") || "").toLowerCase();
  const body = await request.text();

  if (contentType.includes("fdf")) {
    const values = {};
    const re = /\/T\s*\(([^)]*)\)\s*\/V\s*\(([^)]*)\)/g;
    let match;
    while ((match = re.exec(body)) !== null) values[match[1]] = match[2];
    return values;
  }

  const values = {};
  for (const [key, value] of new URLSearchParams(body)) values[key] = value;
  return values;
}

export async function onRequestPost(context) {
  const submitted = await parseSubmission(context.request);
  let baconType = submitted.type || ALLOWED_TYPES[0];
  if (!ALLOWED_TYPES.includes(baconType)) baconType = ALLOWED_TYPES[0];

  let values;
  try {
    const resp = await fetch(BACON_URL + encodeURIComponent(baconType), {
      cf: { cacheTtl: 0 },
    });
    if (!resp.ok) throw new Error("upstream returned " + resp.status);
    const paragraphs = await resp.json();
    const text = paragraphs.join("\r\r");
    values = {
      response: text,
      status: `OK - ${text.length} chars, type=${baconType}`,
    };
  } catch (e) {
    values = { response: "", status: "relay error: " + e.message };
  }

  return new Response(buildFdf(values), {
    headers: {
      "Content-Type": "application/vnd.fdf",
      "Content-Disposition": "inline",
      "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
      "Pragma": "no-cache",
    },
  });
}
