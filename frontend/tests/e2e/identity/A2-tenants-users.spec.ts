// frontend/tests/e2e/identity/A2-tenants-users.spec.ts
import { expect, test } from "@playwright/test";

import { mockAdmin, mockIdentity } from "./fixtures/mock-backend";

test.describe("A2: tenants + users", () => {
  test("tenants list renders rows and navigates to detail", async ({
    page,
  }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["tenant:read"],
    });
    // Boost identity roles so the sidebar's platformOnly check passes.
    await page.route("**/api/me", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          user_id: 1,
          email: "a@b",
          display_name: null,
          avatar_url: null,
          active_tenant_id: 1,
          tenants: [{ id: 1, slug: "default", name: "Default" }],
          workspaces: [],
          permissions: ["tenant:read"],
          roles: { platform: ["platform_admin"] },
        }),
      }),
    );
    await mockAdmin(page, {
      tenants: {
        items: [
          {
            id: 1,
            slug: "acme",
            name: "Acme",
            plan: "pro",
            status: 1,
            created_at: "2026-04-01T00:00:00Z",
          },
        ],
        total: 1,
      },
      tenantDetail: {
        1: {
          id: 1,
          slug: "acme",
          name: "Acme",
          plan: "pro",
          status: 1,
          created_at: "2026-04-01T00:00:00Z",
          member_count: 12,
          workspace_count: 3,
        },
      },
    });

    await page.goto("/admin/tenants");

    // Both slug "acme" and name "Acme" cells exist; confirm the link row first.
    await expect(page.getByRole("link", { name: "acme" })).toBeVisible();
    await page.getByRole("link", { name: "acme" }).click();
    await expect(page).toHaveURL(/\/admin\/tenants\/1$/);
    await expect(
      page.getByRole("heading", { level: 1, name: "Acme" }),
    ).toBeVisible();
    await expect(page.getByText("Members")).toBeVisible();
    await expect(page.getByText("12")).toBeVisible();
    await expect(page.getByText("3")).toBeVisible();
  });

  test("users list shows filter + roles", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["membership:read"],
    });
    await mockAdmin(page, {
      users: {
        items: [
          {
            id: 10,
            email: "alice@deerflow.local",
            display_name: "Alice",
            avatar_url: null,
            status: 1,
            last_login_at: "2026-04-20T00:00:00Z",
            roles: ["tenant_owner", "member"],
          },
        ],
        total: 1,
      },
    });

    await page.goto("/admin/users");

    await expect(
      page.getByRole("cell", { name: "alice@deerflow.local" }),
    ).toBeVisible();
    await expect(page.getByText("tenant_owner")).toBeVisible();
    await expect(page.getByPlaceholder("Filter by email…")).toBeVisible();
  });
});
