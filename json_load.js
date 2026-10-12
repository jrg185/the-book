// JSON for the page. fetch() is the browser path. Node tests read a file
// URL from disk so they do not need a server.

export async function readJsonUrl(url, fetchImpl) {
  const href = String(url);
  if (!fetchImpl && typeof process !== "undefined" && process.versions?.node && href.startsWith("file:")) {
    const { readFileSync } = await import("node:fs");
    const { fileURLToPath } = await import("node:url");
    return JSON.parse(readFileSync(fileURLToPath(href), "utf8"));
  }
  const response = await (fetchImpl || fetch)(url);
  if (!response?.ok) {
    const name = href.split("/").pop().split("?")[0] || "json";
    throw new Error(`${name} did not load`);
  }
  return response.json();
}
