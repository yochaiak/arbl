import assert from "node:assert/strict";
import { once } from "node:events";
import test from "node:test";
import { createVerifierServer } from "./server.js";

const token = "t".repeat(40);
const configured = {
  ABRL_VERIFIER_TOKEN: token,
  ABRL_ADSCORE_ZONE_ID: "41",
  ABRL_ADSCORE_RESPONSE_KEY: Buffer.alloc(32, 7).toString("base64"),
};

async function listen(t, env = configured) {
  const server = await createVerifierServer(env);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  t.after(() => new Promise((resolve) => server.close(resolve)));
  return `http://127.0.0.1:${server.address().port}`;
}

const auth = { Authorization: `Bearer ${token}` };

test("requires service credentials before exposing health or verification", async (t) => {
  const base = await listen(t);
  const response = await fetch(`${base}/health`);
  assert.equal(response.status, 401);
  const health = await fetch(`${base}/health`, { headers: auth });
  assert.deepEqual(await health.json(), {
    state: "pending_smoke_test",
    reason: null,
    last_verified_at: null,
  });
});

test("rejects an unconfigured or incompatible verifier", async (t) => {
  const base = await listen(t, { ...configured, ABRL_ADSCORE_ALGORITHM: "unsupported" });
  const health = await fetch(`${base}/health`, { headers: auth });
  assert.equal((await health.json()).state, "not_configured");
  const response = await fetch(`${base}/verify`, {
    method: "POST", headers: auth,
  });
  assert.equal(response.status, 503);
  assert.equal((await response.json()).verification_status, "unavailable");
});

test("rejects untrusted zone, malformed payload, and unexpected signature version", async (t) => {
  const base = await listen(t);
  const rejected = async (zone_id, signature, reason) => {
    const response = await fetch(`${base}/verify`, {
      method: "POST",
      headers: { ...auth, "Content-Type": "application/json" },
      body: JSON.stringify({ zone_id, signature, ip_address: "192.0.2.10", user_agent: "Test Browser" }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {
      verification_status: "failed", verdict: null, raw_code: null, reason,
    });
  };
  await rejected("42", "BQA", "zone_mismatch");
  await rejected("41", "!!!", "invalid_input");
  await rejected("41", "BAA", "version_mismatch");
  await rejected("41", "BQA", "invalid_signature");
  const health = await fetch(`${base}/health`, { headers: auth });
  assert.equal((await health.json()).state, "pending_smoke_test");
});
