import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

/** Seed the demo tenant once before the browser tests; the script is idempotent. */
export default function globalSetup(): void {
  const webDir = path.dirname(fileURLToPath(import.meta.url));
  const repoRoot = path.resolve(webDir, "..", "..");
  execFileSync("uv", ["run", "python", "scripts/seed_demo.py"], {
    cwd: repoRoot,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
}
