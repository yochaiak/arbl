import { createHash, createPublicKey, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import { createRequire, registerHooks } from "node:module";
import { isIP } from "node:net";
import { pathToFileURL } from "node:url";
import sodium from "libsodium-wrappers";

// The vendor SDK 1.0.9 uses a directory specifier that locutus 3's exports
// resolve as a file. Resolve that one SDK import to locutus's actual index.
const require = createRequire(import.meta.url);
const sdkHook = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (
      specifier === "locutus/php/network" &&
      context.parentURL?.includes("/@adscore/nodejs-common/")
    ) {
      return nextResolve("locutus/php/network/index", context);
    }
    return nextResolve(specifier, context);
  },
});

let sdk;
try {
  sdk = require("@adscore/nodejs-common");
} finally {
  sdkHook.deregister();
}
const { Judge, Signature4, Signature5, VerifyError, VersionError } = sdk;

const SIGNATURE_LIMIT = 16384;
const BODY_LIMIT = 20000;
const ZONE_ID = /^[1-9][0-9]{0,19}$/;
const BASE64 = /^[A-Za-z0-9+/_-]+={0,2}$/;
const ALGORITHMS = new Set(["v5_0101M", "v4", "v4_hashing", "v4_signing"]);
const VERDICTS = new Map([
  [Judge.OK, "clean"],
  [Judge.PU, "potentially_unwanted"],
  [Judge.PROXY, "proxy"],
  [Judge.BOT, "bot"],
]);

class ZoneMismatch extends Error {}

function loadConfig(env) {
  const token = env.ABRL_VERIFIER_TOKEN || "";
  const zoneId = env.ABRL_ADSCORE_ZONE_ID || "";
  const algorithm = env.ABRL_ADSCORE_ALGORITHM || "v5_0101M";
  const keyText = env.ABRL_ADSCORE_RESPONSE_KEY || "";
  if (!token || !zoneId || !keyText) {
    return { token, ready: false, reason: "missing_configuration" };
  }
  if (!ALGORITHMS.has(algorithm) || !ZONE_ID.test(zoneId) || token.length < 32) {
    return { token, ready: false, reason: "invalid_configuration" };
  }
  try {
    if (!BASE64.test(keyText)) throw new Error("Invalid encoding");
    const keyBytes = Buffer.from(keyText, "base64");
    if (keyBytes.length === 0 || (algorithm === "v5_0101M" && keyBytes.length !== 32)) {
      throw new Error("Invalid key size");
    }
    const key = algorithm === "v4_signing"
      ? createPublicKey(keyBytes.toString("ascii").includes("BEGIN PUBLIC KEY")
        ? keyBytes
        : { key: keyBytes, type: "spki", format: "der" })
      : keyBytes;
    return { token, zoneId, algorithm, key, ready: true };
  } catch {
    return { token, ready: false, reason: "invalid_response_key" };
  }
}

function authenticated(req, token) {
  const auth = req.headers.authorization;
  if (!token || typeof auth !== "string" || !auth.startsWith("Bearer ")) {
    return false;
  }
  const digest = (value) => createHash("sha256").update(value).digest();
  return timingSafeEqual(digest(auth.slice(7)), digest(token));
}

function respond(res, status, data) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  });
  res.end(JSON.stringify(data));
}

async function readJson(req) {
  if (req.headers["content-type"]?.split(";")[0] !== "application/json") {
    throw new Error("Invalid content type");
  }
  if (Number(req.headers["content-length"]) > BODY_LIMIT) {
    throw new Error("Request too large");
  }
  const chunks = [];
  let size = 0;
  for await (const chunk of req.iterator({ destroyOnReturn: false })) {
    size += chunk.length;
    if (size > BODY_LIMIT) {
      req.resume();
      throw new Error("Request too large");
    }
    chunks.push(chunk);
  }
  const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("Invalid payload");
  }
  return payload;
}

function failure(reason, verification_status = "failed") {
  return { verification_status, verdict: null, raw_code: null, reason };
}

function verify(payload, config) {
  const { signature, ip_address, user_agent, zone_id } = payload;
  if (zone_id !== config.zoneId) return failure("zone_mismatch");
  if (
    typeof signature !== "string" ||
    signature.length > SIGNATURE_LIMIT ||
    !BASE64.test(signature) ||
    typeof ip_address !== "string" ||
    !isIP(ip_address) ||
    typeof user_agent !== "string" ||
    !user_agent ||
    user_agent.length > 1024 ||
    /[\r\n\x00]/u.test(user_agent)
  ) return failure("invalid_input");

  const bytes = Buffer.from(signature, "base64");
  const expectedVersion = config.algorithm === "v5_0101M" ? 5 : 4;
  if (!bytes.length) return failure("malformed_signature");
  if (bytes[0] !== expectedVersion) return failure("version_mismatch");

  try {
    let parser;
    if (expectedVersion === 5) {
      parser = new Signature5();
      parser.parse(signature, (zoneId) => {
        if (String(zoneId) !== config.zoneId) throw new ZoneMismatch();
        return config.key;
      });
      if (parser.encryptionType !== "Secretbox" || parser.structType !== "Msgpack") {
        return failure("algorithm_mismatch");
      }
      parser.verify([ip_address], user_agent);
    } else {
      parser = new Signature4();
      parser.parse(signature);
      const signType = parser.getPayload()?.customerSignType;
      const expectedSignType = config.algorithm === "v4_signing"
        ? parser.SIGN_SHA256
        : parser.HASH_SHA256;
      if (signType !== expectedSignType) return failure("algorithm_mismatch");
      parser.verify([ip_address], user_agent, config.key);
    }

    const raw_code = parser.getResult();
    if (!Number.isSafeInteger(raw_code)) return failure("invalid_vendor_result");
    return {
      verification_status: "verified",
      verdict: VERDICTS.get(raw_code) || "unknown",
      raw_code,
      reason: null,
    };
  } catch (error) {
    if (error instanceof ZoneMismatch) return failure("zone_mismatch");
    if (error instanceof VersionError) return failure("version_mismatch");
    if (error instanceof VerifyError) return failure("signature_verification_failed");
    return failure("invalid_signature");
  }
}

export async function createVerifierServer(env = process.env) {
  await sodium.ready;
  const config = loadConfig(env);
  let lastVerifiedAt = null;
  return createServer({
    requestTimeout: 10000,
    headersTimeout: 5000,
    keepAliveTimeout: 3000,
  }, async (req, res) => {
    if (!authenticated(req, config.token)) {
      return respond(res, 401, { error: "unauthorized" });
    }
    if (req.method === "GET" && req.url === "/health") {
      const state = !config.ready ? "not_configured" : lastVerifiedAt ? "ready" : "pending_smoke_test";
      return respond(res, 200, { state, reason: config.reason || null, last_verified_at: lastVerifiedAt });
    }
    if (req.method !== "POST" || req.url !== "/verify") {
      return respond(res, 404, { error: "not_found" });
    }
    if (!config.ready) return respond(res, 503, failure(config.reason, "unavailable"));
    let payload;
    try {
      payload = await readJson(req);
    } catch {
      return respond(res, 400, failure("invalid_request"));
    }
    const result = verify(payload, config);
    if (result.verification_status === "verified") {
      lastVerifiedAt = new Date().toISOString();
    }
    return respond(res, 200, result);
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = Number(process.env.ABRL_VERIFIER_PORT || 8001);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("Invalid ABRL_VERIFIER_PORT");
  }
  const server = await createVerifierServer();
  server.listen(port, "127.0.0.1", () => {
    console.info("AdScore verifier listening on loopback");
  });
}
