//one-time (or top-up) seeding for the blank-topic story pool that api/story.js
//serves from. organic traffic refills the pool by one story per blank-topic
//request (see refillPool in api/_lib/relay.js), which is fine for steady state
//but would take dozens of real requests to fill a pool from empty - run this
//once against a fresh redis instead.
//
//  node scripts/seed-pool.mjs [count]
//
//needs the same env vars as the deployed function: HACKCLUB_AI_KEY and either
//the vercel KV or upstash redis pair. `vercel env pull` writes those into
//.env.local; this loads .env and .env.local so you don't have to export them
//by hand.

import fs from "node:fs";
import path from "node:path";
import { generateStory, redisClient, POOL_KEY, POOL_TARGET } from "../api/_lib/relay.js";

function loadDotenv(file) {
  if (!fs.existsSync(file)) return;
  for (const line of fs.readFileSync(file, "utf-8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#") || !trimmed.includes("=")) continue;
    const idx = trimmed.indexOf("=");
    const key = trimmed.slice(0, idx).trim();
    let value = trimmed.slice(idx + 1).trim();
    value = value.replace(/^["']|["']$/g, "");
    //a real environment variable wins, same rule as tools/dev_relay.py
    if (!(key in process.env)) process.env[key] = value;
  }
}

loadDotenv(path.resolve(import.meta.dirname, "../.env"));
loadDotenv(path.resolve(import.meta.dirname, "../.env.local"));

const target = Number(process.argv[2]) || POOL_TARGET;
//generating one at a time (not Promise.all) so a rate limit or a single
//failure doesn't blow up a whole batch at once - a slower fill is fine here,
//this runs once by hand.
async function main() {
  const redis = redisClient();
  const before = await redis.llen(POOL_KEY);
  console.log(`pool has ${before} stories, filling to ${target}...`);

  let added = 0;
  let failed = 0;
  while (before + added < target) {
    try {
      const story = await generateStory("");
      await redis.rpush(POOL_KEY, story);
      added++;
      console.log(`  ${before + added}/${target} (${story.length} chars)`);
    } catch (e) {
      failed++;
      console.error(`  generation failed: ${e.message}`);
      if (failed >= 5) {
        console.error("too many failures, stopping");
        break;
      }
    }
  }
  console.log(`done - pool now has ${before + added} stories`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
