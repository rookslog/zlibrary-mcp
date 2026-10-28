import { test, expect } from "@jest/globals";
import { mkdtempSync, writeFileSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const { getServerVersion } = await import("../dist/index.js");
const pkg = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).version;

function info(body) {
  const file = join(mkdtempSync(join(tmpdir(), "build-info-")), "build-info.json");
  if (body !== undefined) writeFileSync(file, typeof body === "string" ? body : JSON.stringify(body));
  return pathToFileURL(file);
}

test("server version carries the commit dist was built from", () => {
  expect(getServerVersion(info({ commit: "54e8368", dirty: false }))).toBe(`${pkg}+54e8368`);
  expect(getServerVersion(info({ commit: "54e8368", dirty: true }))).toBe(`${pkg}+54e8368.dirty`);
});

test("server version falls back to the package version without usable build info", () => {
  for (const body of [undefined, "not json", { commit: null }, { commit: "zz; rm -rf" }, { commit: 7 }])
    expect(getServerVersion(info(body))).toBe(pkg);
});

test("the build wrote build info, and the default server version uses it", () => {
  const built = JSON.parse(readFileSync(new URL("../build-info.json", import.meta.url), "utf8"));
  expect(built.commit).toMatch(/^[0-9a-f]{7}$/);
  expect(getServerVersion()).toBe(`${pkg}+${built.commit}${built.dirty ? ".dirty" : ""}`);
});
