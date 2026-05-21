// frontend/tests/e2e/identity/A1-session-expired.spec.ts
import { expect, test } from "@playwright/test";

import { mockIdentity } from "./fixtures/mock-backend";

test("session-expired modal appears when /api/me returns 401 mid-session", async ({
  page,
}) => {
  await mockIdentity(page, { authenticated: true });

  // Override the /api/me handler: first call 200 (auth'd profile render), then 401.
  // Registered AFTER mockIdentity so this takes precedence (Playwright matches routes LIFO).
  let firstCall = true;
  await page.route("**/api/me", (route) => {
    if (firstCall) {
      firstCall = false;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 1,
          email: "a@b",
          display_name: "A",
          avatar_url: null,
          active_tenant_id: 1,
          tenants: [{ id: 1, slug: "default", name: "Default" }],
          workspaces: [{ id: 7, slug: "main", name: "Main" }],
          permissions: ["tenant:read"],
          roles: {},
        }),
      });
    }
    return route.fulfill({ status: 401, body: "" });
  });

  await page.goto("/admin/profile");
  await expect(
    page.getByRole("heading", { name: "A", exact: true }),
  ).toBeVisible();

  // Force the /api/me query to refetch via the QueryClient escape hatch exposed
  // in src/app/providers.tsx. The refetch flows through identityFetch, which
  // observes the 401 and emits the session-expired event.
  await page.evaluate(async () => {
    const qc = (
      globalThis as unknown as {
        __DEERFLOW_QC__?: {
          invalidateQueries: (args: { queryKey: unknown[] }) => Promise<void>;
        };
      }
    ).__DEERFLOW_QC__;
    if (!qc) throw new Error("QueryClient not exposed on globalThis");
    await qc.invalidateQueries({ queryKey: ["identity", "me"] });
  });

  await expect(
    page.getByRole("dialog").getByText(/session expired/i),
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByRole("link", { name: /go to sign-in/i }),
  ).toBeVisible();
});
