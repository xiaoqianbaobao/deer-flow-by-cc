// frontend/tests/e2e/identity/A2-workspaces-audit.spec.ts
import { expect, test } from "@playwright/test";

import { mockAdmin, mockIdentity } from "./fixtures/mock-backend";

test.describe("A2: workspaces + audit", () => {
  test("workspaces list → members drill-down", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["workspace:read", "membership:read"],
    });
    await mockAdmin(page, {
      workspaces: {
        items: [
          {
            id: 7,
            tenant_id: 1,
            slug: "main",
            name: "Main",
            description: null,
            created_at: "2026-04-10T00:00:00Z",
            member_count: 4,
          },
        ],
        total: 1,
      },
      workspaceMembers: {
        7: {
          items: [
            {
              id: 11,
              email: "bob@deerflow.local",
              display_name: "Bob",
              avatar_url: null,
              status: 1,
              role: "workspace_admin",
              joined_at: "2026-04-11T00:00:00Z",
            },
          ],
          total: 1,
        },
      },
    });

    await page.goto("/admin/workspaces");
    await expect(page.getByRole("link", { name: /Members →/ })).toBeVisible();
    await page.getByRole("link", { name: /Members →/ }).click();
    await expect(page).toHaveURL(/\/admin\/workspaces\/7\/members$/);
    await expect(
      page.getByRole("cell", { name: "bob@deerflow.local" }),
    ).toBeVisible();
    await expect(page.getByText("workspace_admin")).toBeVisible();
  });

  test("audit page lists rows + paginates via next_cursor", async ({
    page,
  }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["audit:read"],
    });
    await mockAdmin(page, {
      audit: {
        items: [
          {
            id: 1000,
            created_at: "2026-04-20T10:00:00Z",
            tenant_id: 1,
            user_id: 42,
            workspace_id: 7,
            thread_id: null,
            action: "user.login.success",
            resource_type: "user",
            resource_id: "42",
            ip: "10.0.0.1",
            user_agent: "pytest",
            result: "success",
            error_code: null,
            duration_ms: 12,
            metadata: {},
          },
        ],
        next_cursor: "cursor-page-2",
      },
      auditPage2: {
        items: [
          {
            id: 500,
            created_at: "2026-04-19T10:00:00Z",
            tenant_id: 1,
            user_id: 42,
            workspace_id: 7,
            thread_id: null,
            action: "thread.created",
            resource_type: "thread",
            resource_id: "abc",
            ip: "10.0.0.1",
            user_agent: "pytest",
            result: "success",
            error_code: null,
            duration_ms: 3,
            metadata: {},
          },
        ],
        next_cursor: null,
      },
    });

    await page.goto("/admin/audit");
    await expect(page.getByText("user.login.success")).toBeVisible();
    await page.getByRole("button", { name: "Next" }).click();
    await expect(page.getByText("thread.created")).toBeVisible();
    await expect(page.getByRole("button", { name: "Next" })).toBeDisabled();
  });
});
