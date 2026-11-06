(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.DeskLive = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function bytesFromBase64(value) {
    const text = String(value || "");
    const padded = text + "=".repeat((4 - (text.length % 4)) % 4);
    const binary = atob(padded);
    const out = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i);
    return out;
  }

  function relativeName(file) {
    const parts = String(file || "").split("/").map((part) => decodeURIComponent(part));
    if (!parts.length || parts.some((part) => !part || part === "." || part === "..")) {
      throw new Error("live path");
    }
    return parts.join("/");
  }

  function encodedPath(relative) {
    return relative.split("/").map((part) => encodeURIComponent(part)).join("/");
  }

  async function decryptLiveEnvelope(envelope, dataPath, name) {
    if (!envelope || envelope.v !== 1 || envelope.alg !== "AES-256-GCM" || !envelope.n || !envelope.c) {
      throw new Error("live decrypt failed");
    }
    if (!dataPath || !name) throw new Error("live decrypt failed");
    try {
      const keyBytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(dataPath));
      const key = await crypto.subtle.importKey("raw", keyBytes, { name: "AES-GCM" }, false, ["decrypt"]);
      const plain = await crypto.subtle.decrypt(
        {
          name: "AES-GCM",
          iv: bytesFromBase64(envelope.n),
          additionalData: new TextEncoder().encode(name),
        },
        key,
        bytesFromBase64(envelope.c)
      );
      return JSON.parse(new TextDecoder().decode(plain));
    } catch (err) {
      throw new Error("live decrypt failed");
    }
  }

  async function loadDeskFile(file, options) {
    const settings = options || {};
    const dataPath = settings.dataPath || "";
    if (!dataPath) throw new Error("missing");
    const name = relativeName(file);
    const fetchImpl = settings.fetchImpl || fetch;
    const pageUrl = settings.pageUrl || (typeof location !== "undefined" ? location.href : "");
    const base = new URL(".", pageUrl);
    const plainUrl = new URL("d/" + encodeURIComponent(dataPath) + "/" + encodedPath(name), base);
    const plain = await fetchImpl(plainUrl, { cache: "no-store" });
    if (plain.ok) return plain.json();
    if (plain.status !== 404) throw new Error(String(plain.status));
    const liveUrl = new URL("live/" + encodedPath(name), base);
    const live = await fetchImpl(liveUrl, { cache: "no-store" });
    if (!live.ok) throw new Error(String(live.status));
    return decryptLiveEnvelope(await live.json(), dataPath, name);
  }

  return {
    decryptLiveEnvelope: decryptLiveEnvelope,
    loadDeskFile: loadDeskFile,
    relativeName: relativeName,
  };
});
