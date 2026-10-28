// Records which commit dist/ was built from (kept outside dist/, which must match a plain tsc build), so the server can report it (see getServerVersion).
import { execFileSync } from "node:child_process";
import { realpathSync, writeFileSync } from "node:fs";
import { getRuntimeDigest } from "../dist/lib/build-info.js";
import { join } from "node:path";

const root = realpathSync(new URL("../", import.meta.url));
// Prevent caller environment from redirecting Git into an unrelated repository.
const gitEnv = Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith("GIT_")));

function git(args) {
  try {
    return execFileSync("git", args, { cwd: root, env: gitEnv, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return null;
  }
}

const top = git(["rev-parse", "--show-toplevel"]);
const commit = top !== null && realpathSync(top) === root ? git(["rev-parse", "--short=7", "HEAD"]) : null;
const runtime_sha256 = getRuntimeDigest(join(root, "dist"));
const dirty = commit !== null && git(["status", "--porcelain", "--", "src", "lib", "package.json"]) !== "";
writeFileSync(
  new URL("../build-info.json", import.meta.url),
  JSON.stringify({ commit, dirty, runtime_sha256, built_at: new Date().toISOString() }) + "\n",
);
console.log(`build-info: ${commit ?? "no git"}${dirty ? " (dirty)" : ""}`);
