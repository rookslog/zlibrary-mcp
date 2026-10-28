// Records which commit dist/ was built from (kept outside dist/, which must match a plain tsc build), so the server can report it (see getServerVersion).
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";

function git(args) {
  try {
    return execFileSync("git", args, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return null;
  }
}

const commit = git(["rev-parse", "--short=7", "HEAD"]);
const dirty = commit !== null && git(["status", "--porcelain", "--", "src", "lib", "package.json"]) !== "";
writeFileSync(
  new URL("../build-info.json", import.meta.url),
  JSON.stringify({ commit, dirty, built_at: new Date().toISOString() }) + "\n",
);
console.log(`build-info: ${commit ?? "no git"}${dirty ? " (dirty)" : ""}`);
