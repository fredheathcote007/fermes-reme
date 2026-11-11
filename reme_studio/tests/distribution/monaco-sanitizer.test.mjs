import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = new URL("../../", import.meta.url);
const expectedVersion = JSON.parse(
  await readFile(
    new URL("../../node_modules/dompurify/package.json", import.meta.url),
    "utf8",
  ),
).version;

// The Monaco editor bundles its own DOMPurify source unless the Vite plugin
// redirects that module. Verify the actual shipped JS, not only npm lockfiles.
async function javascriptFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const filename = path.join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await javascriptFiles(filename)));
    if (entry.isFile() && entry.name.endsWith(".js")) files.push(filename);
  }
  return files;
}

test("Studio bundles Monaco with the pinned DOMPurify sanitizer", async () => {
  for (const relativeDir of [
    "dist-static/assets",
    "dist-demo/assets",
    "dist/client/_next/static/chunks",
  ]) {
    const directory = fileURLToPath(new URL(relativeDir + "/", root));
    const files = await javascriptFiles(directory);
    let foundSanitizer = false;
    for (const file of files) {
      const content = await readFile(file, "utf8");
      if (!content.includes("TRUSTED_TYPES_POLICY callback")) continue;
      foundSanitizer = true;
      assert.ok(
        content.includes("version=`" + expectedVersion + "`"),
        `${relativeDir}: Monaco sanitiser bundle must match pinned DOMPurify ${expectedVersion}`,
      );
      assert.doesNotMatch(content, /version=`3\.2\.7`|version=`3\.4\.15`/);
    }
    assert.ok(
      foundSanitizer,
      `${relativeDir}: Monaco sanitiser bundle must be present`,
    );
  }
});
