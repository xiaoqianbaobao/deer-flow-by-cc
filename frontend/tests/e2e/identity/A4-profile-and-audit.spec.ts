// frontend/tests/e2e/identity/A4-profile-and-audit.spec.ts
import { expect, test } from "@playwright/test";

import {
  mockAdmin,
  mockIdentity,
  mockWrites,
} from "./fixtures/mock-backend";

test.describe("A4: profile + audit polish", () => {
  test("profile tabs render basic, my tokens, my sessions", async ({ page }) => {
    await mockIdentity(page, { authenticated: true });
    await mockWrites(page, {
      myTokens: [
        {
          id: 1,
          name: "personal-cli",
          prefix: "dft_personal",
          scopes: ["skill:invoke"],
          workspace_id: null,
          created_at: "2026-04-10T00:00:00Z",
          expires_at: null,
          last_used_at: "2026-04-23T00:00:00Z",
        },
      ],
      mySessions: [
        {
          sid: "sess-abc-123",
          created_at: "2026-04-22T08:00:00Z",
          ip: "10.0.0.1",
          user_agent: "Mozilla/5.0 (Test)",
        },
      ],
    });

    await page.goto("/admin/profile");

    // Basic tab is the default.
    await expect(page.getByText("Active tenant")).toBeVisible();

    await page.getByTestId("profile-tab-tokens").click();
    await expect(page.getByTestId("my-tokens-tab")).toBeVisible();
    await expect(page.getByText("personal-cli")).toBeVisible();
    await expect(page.getByText("dft_personal")).toBeVisible();

    await page.getByTestId("profile-tab-sessions").click();
    await expect(page.getByTestId("my-sessions-tab")).toBeVisible();
    await expect(page.getByText("10.0.0.1")).toBeVisible();
  });

  test("create personal token shows plaintext", async ({ page }) => {
    await mockIdentity(page, { authenticated: true });
    const rec = await mockWrites(page, { myTokens: [] });

    await page.goto("/admin/profile");
    await page.getByTestId("profile-tab-tokens").click();
    await page.getByTestId("my-token-new-btn").click();
    await page.getByTestId("my-token-name-input").fill("personal");
    await page.getByTestId("my-token-submit-btn").click();

    await expect(page.getByTestId("my-token-plaintext-dialog")).toBeVisible();
    await expect(page.getByTestId("my-token-plaintext-value")).toHaveValue(
      "dft_MY_PLAINTEXT_abc",
    );
    expect(rec.createMyToken).toHaveLength(1);
    expect(rec.createMyToken[0]?.body).toMatchObject({ name: "personal" });
  });

  test("revoke personal session sends DELETE", async ({ page }) => {
    await mockIdentity(page, { authenticated: true });
    const rec = await mockWrites(page, {
      myTokens: [],
      mySessions: [
        {
          sid: "sess-to-revoke",
          created_at: "2026-04-22T08:00:00Z",
          ip: "10.0.0.1",
          user_agent: "Mozilla/5.0",
        },
      ],
    });

    await page.goto("/admin/profile");
    await page.getByTestId("profile-tab-sessions").click();
    await page.getByTestId("my-session-revoke-sess-to-revoke").click();

    await expect.poll(() => rec.revokeMySession.length).toBe(1);
    expect(rec.revokeMySession[0]?.url).toContain(
      "/api/me/sessions/sess-to-revoke",
    );
  });

  test("audit row click opens detail drawer with metadata", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["audit:read"],
    });
    await mockAdmin(page, {
      audit: {
        items: [
          {
            id: 555,
            created_at: "2026-04-20T12:34:56Z",
            tenant_id: 1,
            user_id: 42,
            workspace_id: 7,
            thread_id: "th-xyz",
            action: "tool.called",
            resource_type: "tool",
            resource_id: "bash",
            ip: "10.1.1.1",
            user_agent: "agent/1.0",
            result: "success",
            error_code: null,
            duration_ms: 120,
            metadata: { command: "ls -la", path: "/mnt/work" },
          },
        ],
        next_cursor: null,
      },
    });

    await page.goto("/admin/audit");
    await page.getByTestId("audit-row-555").click();

    await expect(page.getByTestId("audit-detail-dialog")).toBeVisible();
    await expect(page.getByTestId("audit-detail-metadata")).toContainText(
      "ls -la",
    );
    await expect(page.getByText("Event 555")).toBeVisible();
  });

  test("audit export link reflects current filters", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["audit:read"],
    });
    await mockAdmin(page, {
      audit: { items: [], next_cursor: null },
    });

    await page.goto("/admin/audit");
    await page.getByTestId("audit-action-filter").fill("user.login.success");

    const link = page.getByTestId("audit-export-link");
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute(
      "href",
      /\/api\/tenants\/1\/audit\/export\?action=user\.login\.success/,
    );
  });
});
