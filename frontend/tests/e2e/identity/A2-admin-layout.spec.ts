// frontend/tests/e2e/identity/A2-admin-layout.spec.ts
import { expect, test } from "@playwright/test";

import { mockAdmin, mockIdentity } from "./fixtures/mock-backend";

test.describe("A2: admin shell", () => {
  test("sidebar shows only links the user has permission for (tenant_owner)", async ({
    page,
  }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: [
        "membership:read",
        "workspace:read",
        "token:read",
        "audit:read",
      ],
    });
    await mockAdmin(page);

    await page.goto("/admin/profile");

    // Always-visible items
    await expect(page.getByRole("link", { name: "Profile" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Roles" })).toBeVisible();
    // Permission-gated (tenant_owner has all these)
    await expect(page.getByRole("link", { name: "Users" })).toBeVisible();
    await expect(
      page.getByRole("link", { name: "Workspaces" }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Tokens" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Audit" })).toBeVisible();
    // Platform-only link must be hidden (no platform_admin role).
    await expect(page.getByRole("link", { name: "Tenants" })).toHaveCount(0);
  });

  test("platform_admin sees the Tenants link", async ({ page }) => {
    await mockIdentity(page, { authenticated: true });
    // mockIdentity default roles omit "platform"; override /api/me to add it.
    await page.route("**/api/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 42,
          email: "admin@deerflow.local",
          display_name: "Admin",
          avatar_url: null,
          active_tenant_id: 1,
          tenants: [
            { id: 1, slug: "default", name: "Default" },
            { id: 2, slug: "acme", name: "Acme" },
          ],
          workspaces: [{ id: 7, slug: "main", name: "Main" }],
          permissions: [
            "tenant:read",
            "membership:read",
            "workspace:read",
            "token:read",
            "audit:read",
          ],
          roles: { platform: ["platform_admin"] },
        }),
      }),
    );
    await mockAdmin(page);

    await page.goto("/admin/profile");

    await expect(page.getByRole("link", { name: "Tenants" })).toBeVisible();
  });

  test("tenant switcher appears for multi-tenant user", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      tenants: [
        { id: 1, slug: "default", name: "Default" },
        { id: 2, slug: "acme", name: "Acme" },
      ],
    });
    await mockAdmin(page);

    await page.goto("/admin/profile");

    await expect(
      page.getByRole("button", { name: /switch tenant/i }),
    ).toBeVisible();
  });
});
