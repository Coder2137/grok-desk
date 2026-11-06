const { decryptLiveEnvelope, loadDeskFile } = require("../web/live.js");
const fs = require("fs");

const command = process.argv[2];
const secret = process.argv[3] || "";

function fail(message) {
  process.stderr.write(message + "\n");
  process.exit(1);
}

async function main() {
  if (command === "decrypt") {
    const envelope = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
    const name = process.argv[5];
    const value = await decryptLiveEnvelope(envelope, secret, name);
    process.stdout.write(JSON.stringify(value));
    return;
  }
  if (command === "reject") {
    const envelope = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
    const name = process.argv[5];
    try {
      await decryptLiveEnvelope(envelope, secret, name);
    } catch (err) {
      if (err && err.message === "live decrypt failed" && !String(err.message).includes(secret)) {
        process.stdout.write("rejected");
        return;
      }
    }
    fail("expected decrypt to fail");
  }
  if (command === "prefer-plain") {
    let liveFetched = false;
    const expected = "https://example.test/grok-desk/d/" + encodeURIComponent(secret) + "/state.json";
    const value = await loadDeskFile("state.json", {
      dataPath: secret,
      pageUrl: "https://example.test/grok-desk/#d=" + encodeURIComponent(secret),
      fetchImpl: async (url) => {
        const href = String(url);
        if (href.includes("/live/")) liveFetched = true;
        if (href !== expected) return { ok: false, status: 404, json: async () => ({}) };
        return { ok: true, status: 200, json: async () => ({ balance: 3, from: "plain" }) };
      },
    });
    if (liveFetched) fail("plaintext response fetched live");
    if (value.from !== "plain" || value.balance !== 3) fail("plaintext body was not used");
    process.stdout.write("plain");
    return;
  }
  if (command === "fallback") {
    const envelope = JSON.parse(fs.readFileSync(process.argv[4], "utf8"));
    const expectedPlain = "https://example.test/grok-desk/d/" + encodeURIComponent(secret) + "/coin/Mint111.json";
    const expectedLive = "https://example.test/grok-desk/live/coin/Mint111.json";
    const seen = [];
    const value = await loadDeskFile("coin/" + encodeURIComponent("Mint111") + ".json", {
      dataPath: secret,
      pageUrl: "https://example.test/grok-desk/index.html",
      fetchImpl: async (url) => {
        const href = String(url);
        seen.push(href);
        if (href === expectedPlain) return { ok: false, status: 404, json: async () => ({}) };
        if (href === expectedLive) {
          return { ok: true, status: 200, json: async () => envelope };
        }
        return { ok: false, status: 404, json: async () => ({}) };
      },
    });
    if (!seen.includes(expectedPlain) || !seen.includes(expectedLive)) fail("url mismatch");
    if (expectedLive.includes(secret)) fail("live url contained the path");
    if (value.ok !== true) fail("fallback plaintext mismatch");
    process.stdout.write("fallback");
    return;
  }
  if (command === "status") {
    let liveFetched = false;
    try {
      await loadDeskFile("state.json", {
        dataPath: secret,
        pageUrl: "https://example.test/grok-desk/",
        fetchImpl: async (url) => {
          if (String(url).includes("/live/")) liveFetched = true;
          return { ok: false, status: 500, json: async () => ({}) };
        },
      });
    } catch (err) {
      if (!liveFetched && err && err.message === "500") {
        process.stdout.write("status");
        return;
      }
    }
    fail("expected the plaintext status to stop the fallback");
  }
  if (command === "snapshot") {
    const name = process.argv[4];
    const envelope = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
    if (envelope.v !== 1 || envelope.alg !== "AES-256-GCM") fail("snapshot shape");
    try {
      await decryptLiveEnvelope(envelope, secret, name);
    } catch (err) {
      if (err && err.message === "live decrypt failed") {
        process.stdout.write("rejected");
        return;
      }
    }
    fail("snapshot decrypted with the fixture path");
  }
  fail("unknown command");
}

main().catch(() => fail("decrypt failed"));
