//cloudflare pages function - the production counterpart of tools/dev_relay.py.
//
//the pdf cannot call the model directly. the only network api acrobat exposes to
//a plain document is doc.submitForm(), whose response must come back as fdf for
//acrobat to import it into form fields. so this endpoint takes the pdf's form
//post, performs the real api call, and re-wraps the completion as fdf.
//
//it also holds the api key. anyone who opens a pdf can read every byte of it, so
//a key embedded in the document is a published key. set it as a secret:
//  npx wrangler pages secret put HACKCLUB_AI_KEY
//
//KNOWN LIMITATION - this still calls the model inline and so does NOT yet match
//tools/dev_relay.py. acrobat drops a submitForm connection long before a 15-180
//second generation finishes (ConnectionResetError / WinError 10054 server side),
//which is why the dev relay answers immediately with a job id and lets the pdf
//poll. reproducing that here needs somewhere to keep job state between requests,
//because a pages function is stateless and its isolate can be recycled between
//polls - a KV namespace binding is the usual answer. until that is done, this
//endpoint only works if generation happens to finish inside acrobat's tolerance.
//the pdf's polling logic is already compatible: it just needs this to return a
//"WORKING" status plus a job id instead of blocking.

const DEFAULT_AI_URL = "https://ai.hackclub.com/proxy/v1/chat/completions";
//the leading ~ is part of the model id on this proxy - it marks a floating alias.
//without it the proxy returns "is not a valid model ID".
const DEFAULT_AI_MODEL = "~deepseek/deepseek-v4-flash-latest";

//keep in sync with tools/dev_relay.py
const BASE_PROMPT =
  "Think of something you haven't thought of before. Try your best to be random. " +
  "Try to decide if your text is like the number 7 or not. Then decide a story. " +
  "Something obscene. Under 300 words. Make it weird and goofy, but not " +
  "offputting. It should feel sloppy, but not too sloppy.";

const MAX_TOPIC_LEN = 200;

//this has to cover reasoning tokens as well as the story. the default model burns
//300-900 tokens thinking before it writes anything, and if the budget runs out
//first the api returns finish_reason=length with content=null - no story at all.
const DEFAULT_MAX_TOKENS = 3000;

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
    .replace(/\n/g, "\\r");
}

//a pdf literal string can only carry the font's 8 bit encoding, and model output
//is full of em dashes and smart quotes. fall back to a utf-16be hex string (the
//leading BOM is what tells acrobat it is unicode) when the text is not ascii.
//ascii still takes the literal path so the fdf stays readable while debugging.
function pdfTextString(text) {
  const normalised = text.replace(/\r\n|\r/g, "\n");

  if (/^[\x00-\x7F]*$/.test(normalised)) {
    return "(" + pdfEscape(normalised) + ")";
  }

  //"feff" is the utf-16 BOM, which is what marks this as a unicode text string
  let hex = "feff";
  for (const char of normalised.replace(/\n/g, "\r")) {
    const code = char.codePointAt(0);
    if (code > 0xffff) {
      //surrogate pair - utf-16be stores both halves
      const adjusted = code - 0x10000;
      hex += (0xd800 + (adjusted >> 10)).toString(16).padStart(4, "0");
      hex += (0xdc00 + (adjusted & 0x3ff)).toString(16).padStart(4, "0");
    } else {
      hex += code.toString(16).padStart(4, "0");
    }
  }
  return "<" + hex + ">";
}

//no /F or /UF key here - adobe's enhanced security rules require its absence
//for a pdf that was opened from the local filesystem.
function buildFdf(values) {
  const fields = Object.entries(values)
    .map(([name, value]) => `<< /T${pdfTextString(name)} /V${pdfTextString(value)} >>`)
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

function buildPrompt(topic) {
  let prompt = BASE_PROMPT;
  if (topic) prompt += `\n\nWork this topic in somewhere: ${topic}`;
  //the model is asked to be random, so give it something to be random from -
  //this also stops any layer in between serving a cached completion.
  const entropy = crypto.randomUUID().slice(0, 8);
  prompt += `\n\n(entropy: ${entropy} - ignore this token, it only exists to vary your output)`;
  return prompt;
}

export async function onRequestPost(context) {
  const submitted = await parseSubmission(context.request);
  const topic = (submitted.topic || "").trim().slice(0, MAX_TOPIC_LEN);

  const key = context.env.HACKCLUB_AI_KEY;
  const url = context.env.HACKCLUB_AI_URL || DEFAULT_AI_URL;
  const model = context.env.HACKCLUB_AI_MODEL || DEFAULT_AI_MODEL;
  const maxTokens = Number(context.env.HACKCLUB_AI_MAX_TOKENS) || DEFAULT_MAX_TOKENS;

  let values;
  try {
    if (!key) throw new Error("HACKCLUB_AI_KEY is not set");

    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model: model,
        messages: [{ role: "user", content: buildPrompt(topic) }],
        temperature: 1.0,
        max_tokens: maxTokens,
      }),
    });

    if (!resp.ok) {
      const detail = (await resp.text()).slice(0, 200);
      throw new Error(`upstream ${resp.status}: ${detail}`);
    }

    const body = await resp.json();
    const choice = body.choices[0];
    const story = (choice.message.content || "").trim();

    //a reasoning model that exhausts max_tokens while still thinking returns
    //finish_reason=length with a null content, which is otherwise a baffling
    //failure - name it so the pdf's status field says something useful.
    if (!story) {
      const used = choice.finish_reason;
      if (used === "length") {
        throw new Error(`model used all ${maxTokens} tokens on reasoning and ` +
                        `never wrote the story - raise HACKCLUB_AI_MAX_TOKENS`);
      }
      throw new Error(`model returned no content (finish_reason=${used})`);
    }

    values = {
      response: story,
      status: `OK - ${story.length} chars${topic ? ", topic: " + topic : ""}`,
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
