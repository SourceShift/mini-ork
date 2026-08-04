/**
 * Cryptographically-strong lowercase hex string of `byteLength` random bytes.
 *
 * Uses Web Crypto (`crypto.getRandomValues`) rather than `Math.random`, whose
 * output is predictable and must never seed anything id- or token-like. Web
 * Crypto ships in every browser and in Node 16+ / jsdom, so this is safe in
 * app and test runtimes alike. Callers use it for locally-unique ids (backend
 * keys, pending message/event ids); unguessability is a bonus, uniqueness is
 * the requirement.
 */
export function randomHex(byteLength = 8): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.getRandomValues === "function"
  ) {
    const bytes = new Uint8Array(byteLength);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }
  // Web Crypto entirely absent (ancient / non-browser runtime). Fall back to a
  // time-derived hex string — not unguessable, but this is only ever a local
  // uniqueness token, and every supported target ships Web Crypto anyway.
  return Date.now().toString(16).padStart(byteLength * 2, "0");
}
