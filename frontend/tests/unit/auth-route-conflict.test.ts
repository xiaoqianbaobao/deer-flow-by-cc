import { existsSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const thisDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(thisDir, "..", "..");

describe("frontend auth route layout", () => {
  it("does not shadow gateway auth endpoints with a Next.js /api/auth catch-all route", () => {
    const conflictingCatchAllRoute = resolve(
      frontendRoot,
      "src",
      "app",
      "api",
      "auth",
      "[...all]",
      "route.ts",
    );

    expect(existsSync(conflictingCatchAllRoute)).toBe(false);
  });
});
