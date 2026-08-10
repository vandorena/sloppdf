//vercel serverless function - the deployed counterpart of tools/dev_relay.py.
//
//the pdf cannot call the model directly. the only network api acrobat exposes to
//a plain document is doc.submitForm(), whose response must come back as fdf for
//acrobat to import it into form fields. so this endpoint takes the pdf's form
//post, performs the real api call, and re-wraps the completion as fdf.
//
//it also holds the api key. anyone who opens a pdf can read every byte of it, so
//a key embedded in the document is a published key.
//
//two things force the shape of this file:
//
//  1. acrobat drops a submitForm connection long before a generation finishes, so
//     this must answer in milliseconds. it starts a job, replies with the job id,
//     and generation continues after the response via waitUntil().
//  2. vercel functions are stateless and two polls will not hit the same
//     instance, so the job state lives in redis rather than in memory.
//
//env vars:
//  HACKCLUB_AI_KEY                          required
//  KV_REST_API_URL / KV_REST_API_TOKEN      set by the vercel kv integration
//  UPSTASH_REDIS_REST_URL / ..._TOKEN       set by the upstash integration
//  HACKCLUB_AI_MODEL, HACKCLUB_AI_MAX_TOKENS, HACKCLUB_AI_URL   optional

import { waitUntil } from "@vercel/functions";
import { redisClient, generateStory, refillPool, POOL_KEY } from "./_lib/relay.js";

const MAX_TOPIC_LEN = 200;
const JOB_TTL = 900;      //seconds a job survives in redis
const JOB_DEADLINE = 55;  //seconds before a running job is declared dead

// ---------------------------------------------------------------- fdf encoding

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

// ------------------------------------------------------------- request parsing

//vercel only parses body types it recognises, and application/vnd.fdf is not one
//of them, so read the raw stream and parse it here.
function readRawBody(req) {
  return new Promise((resolve, reject) => {
    let data = "";
    req.on("data", (chunk) => { data += chunk; });
    req.on("end", () => resolve(data));
    req.on("error", reject);
  });
}

//acrobat posts fdf by default, or urlencoded when cSubmitAs is "HTML".
function parseSubmission(body, contentType) {
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

// ------------------------------------------------------------------------ jobs

//the pdf's document script polls on the "WORKING" prefix, so these strings are
//part of the contract - see poll_once() in httpdemo.js.
function jobFields(id, job) {
  if (!job) return { response: "", status: "relay error: unknown or expired job", job: "" };

  const waited = Math.round((Date.now() - job.started) / 1000);

  if (job.state === "running") {
    //waitUntil work dies with the function, so a job past the deadline is never
    //going to finish. say so rather than letting the pdf poll for five minutes.
    if (waited > JOB_DEADLINE) {
      return {
        response: "",
        status: "relay error: generation exceeded the function time limit - " +
                "try a faster model",
        job: "",
      };
    }
    return { status: `WORKING - generating, ${waited}s elapsed...`, job: id };
  }
  if (job.state === "error") {
    return { response: "", status: `relay error: ${job.error}`, job: "" };
  }
  return {
    response: job.story,
    status: `OK - ${job.story.length} chars in ${waited}s`,
    job: "",
  };
}

//stores a running job, kicks off generation in the background, and returns
//[id, job] so the caller can render the just-started state without a second
//redis round trip.
async function startJob(redis, topic) {
  const id = crypto.randomUUID().replace(/-/g, "").slice(0, 12);
  const job = { state: "running", started: Date.now(), story: "", error: "" };
  await redis.set(`job:${id}`, job, { ex: JOB_TTL });

  waitUntil(
    generateStory(topic)
      .then((story) => redis.set(`job:${id}`,
        { ...job, state: "done", story }, { ex: JOB_TTL }))
      .catch((e) => redis.set(`job:${id}`,
        { ...job, state: "error", error: e.message }, { ex: JOB_TTL }))
  );

  return [id, job];
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).send("POST only\n");
    return;
  }

  const raw = await readRawBody(req);
  const submitted = parseSubmission(raw, (req.headers["content-type"] || "").toLowerCase());
  const topic = (submitted.topic || "").trim().slice(0, MAX_TOPIC_LEN);
  const jobId = (submitted.job || "").trim();

  let values;
  try {
    const redis = redisClient();

    if (jobId) {
      //a poll
      values = jobFields(jobId, await redis.get(`job:${jobId}`));
    } else if (!topic) {
      //a fresh request with no topic - these are interchangeable, so skip the
      //job/poll dance entirely and hand back a story that's already sitting in
      //redis. LPOP is atomic, so two blank-topic requests racing each other
      //never get the same story.
      const cached = await redis.lpop(POOL_KEY);
      if (cached) {
        waitUntil(refillPool(redis));
        values = { response: cached, status: `OK - ${cached.length} chars (prerendered)`, job: "" };
      } else {
        //pool's dry - fall back to a normal job, and try to refill for next time
        waitUntil(refillPool(redis));
        values = jobFields(...await startJob(redis, topic));
      }
    } else {
      values = jobFields(...await startJob(redis, topic));
    }
  } catch (e) {
    values = { response: "", status: `relay error: ${e.message}`, job: "" };
  }

  res.setHeader("Content-Type", "application/vnd.fdf");
  res.setHeader("Content-Disposition", "inline");
  res.setHeader("Cache-Control", "no-cache, no-store, max-age=0, must-revalidate");
  res.setHeader("Pragma", "no-cache");
  res.status(200).send(buildFdf(values));
}
