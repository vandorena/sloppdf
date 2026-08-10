//shared between api/story.js and scripts/seed-pool.mjs. anything both need to
//generate a story or talk to redis lives here so the two never drift apart.

import { Redis } from "@upstash/redis";

export const DEFAULT_AI_URL = "https://ai.hackclub.com/proxy/v1/chat/completions";

//a fast non-reasoning model on purpose. the reasoning models this proxy offers
//take 15-180s, and a hobby-plan function is killed at 60s - a slow generation
//would be cut off mid-flight and the job would never complete. override with
//HACKCLUB_AI_MODEL if you move to a plan with a longer ceiling.
export const DEFAULT_AI_MODEL = "qwen/qwen3-32b";

//keep in sync with tools/dev_relay.py
export const BASE_PROMPT =
  "Think of something you haven't thought of before. Try your best to be random. " +
  "Try to decide if your text is like the number 7 or not. Then decide a story. " +
  "Something obscene. Under 300 words. Make it weird and goofy, but not " +
  "offputting. It should feel sloppy, but not too sloppy.";

export const DEFAULT_MAX_TOKENS = 3000;

//the key blank-topic requests are prerendered under, and how many should stay
//in stock. one story is popped and replaced per blank-topic request, so this
//is a steady-state target, not a hard cap enforced up front - see
//docs/prerender-pool.md for the seeding step that gets a fresh redis to this
//size before relying on organic traffic to sustain it.
export const POOL_KEY = "story-pool";
export const POOL_TARGET = 30;

//the vercel kv and upstash integrations export different names for the same pair
export function redisClient() {
  const url = process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN;
  if (!url || !token) throw new Error("no redis credentials in the environment");
  return new Redis({ url, token });
}

export function buildPrompt(topic) {
  let prompt = BASE_PROMPT;
  if (topic) prompt += `\n\nWork this topic in somewhere: ${topic}`;
  //the model is asked to be random, so give it something to be random from -
  //this also stops any layer in between serving a cached completion.
  prompt += `\n\n(entropy: ${crypto.randomUUID().slice(0, 8)} - ignore this token, ` +
            `it only exists to vary your output)`;
  return prompt;
}

export async function generateStory(topic) {
  const key = process.env.HACKCLUB_AI_KEY;
  if (!key) throw new Error("HACKCLUB_AI_KEY is not set");

  const resp = await fetch(process.env.HACKCLUB_AI_URL || DEFAULT_AI_URL, {
    method: "POST",
    headers: {
      "Authorization": "Bearer " + key,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model: process.env.HACKCLUB_AI_MODEL || DEFAULT_AI_MODEL,
      messages: [{ role: "user", content: buildPrompt(topic) }],
      temperature: 1.0,
      max_tokens: Number(process.env.HACKCLUB_AI_MAX_TOKENS) || DEFAULT_MAX_TOKENS,
    }),
  });

  if (!resp.ok) {
    throw new Error(`upstream ${resp.status}: ${(await resp.text()).slice(0, 200)}`);
  }

  const body = await resp.json();
  const choice = body.choices[0];
  const story = (choice.message.content || "").trim();

  //a reasoning model that exhausts max_tokens while still thinking returns
  //finish_reason=length with a null content, which is otherwise a baffling
  //failure - name it so the caller can say something useful.
  if (!story) {
    if (choice.finish_reason === "length") {
      throw new Error("model spent its whole token budget on reasoning and never " +
                      "wrote the story - raise HACKCLUB_AI_MAX_TOKENS");
    }
    throw new Error(`model returned no content (finish_reason=${choice.finish_reason})`);
  }
  return story;
}

//tops the pool up by exactly one story, if it isn't already at target. called
//after every blank-topic request (hit or miss) so the pool trends back toward
//POOL_TARGET without a separate cron job. swallows errors - a failed refill
//just means the pool stays a little smaller until the next request tries again.
export async function refillPool(redis) {
  try {
    const len = await redis.llen(POOL_KEY);
    if (len >= POOL_TARGET) return;
    const story = await generateStory("");
    await redis.rpush(POOL_KEY, story);
  } catch (e) {
    console.error("pool refill failed:", e.message);
  }
}
