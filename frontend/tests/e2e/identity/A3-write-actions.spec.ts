// frontend/tests/e2e/identity/A3-write-actions.spec.ts
import { expect, test } from "@playwright/test";

import {
  mockAdmin,
  mockIdentity,
  mockWrites,
} from "./fixtures/mock-backend";

test.describe("A3: write actions", () => {
  test("create user dialog posts payload and closes on success", async ({
    page,
  }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["membership:read", "membership:invite"],
    });
    await mockAdmin(page, {
      users: { items: [], total: 0 },
    });
    const rec = await mockWrites(page);

    await page.goto("/admin/users");
    await page.getByTestId("users-new-btn").click();

    const dialog = page.getByTestId("users-create-dialog");
    await expect(dialog).toBeVisible();

    await page.getByTestId("users-create-email").fill("new@example.com");
    await page.getByTestId("users-create-display-name").fill("New User");
    await page.getByTestId("users-create-submit").click();

    await expect(dialog).toBeHidden();
    expect(rec.createUser).toHaveLength(1);
    expect(rec.createUser[0]?.body).toEqual({
      email: "new@example.com",
      display_name: "New User",
    });
  });

  test("create user dialog surfaces 409 inline", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["membership:read", "membership:invite"],
    });
    await mockAdmin(page, { users: { items: [], total: 0 } });
    await mockWrites(page, { failCreateUser: true });

    await page.goto("/admin/users");
    await page.getByTestId("users-new-btn").click();
    await page.getByTestId("users-create-email").fill("dup@example.com");
    await page.getByTestId("users-create-submit").click();
    await expect(
      page.getByText(/Could not create user/i),
    ).toBeVisible();
    // Dialog stays open so the user can retry.
    await expect(page.getByTestId("users-create-dialog")).toBeVisible();
  });

  test("create tenant token shows plaintext once and offers copy", async ({
    page,
  }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["token:read", "token:create", "token:revoke"],
    });
    await mockAdmin(page, {
      tokens: { items: [], total: 0 },
    });
    const rec = await mockWrites(page);

    await page.goto("/admin/tokens");
    await page.getByTestId("tokens-new-btn").click();
    await page.getByTestId("token-name-input").fill("ci-bot");
    await page.getByTestId("token-scopes-input").fill("skill:invoke");
    await page.getByTestId("token-submit-btn").click();

    const plaintextDialog = page.getByTestId("token-plaintext-dialog");
    await expect(plaintextDialog).toBeVisible();
    await expect(page.getByTestId("token-plaintext-value")).toHaveValue(
      "dft_PLAINTEXT_ONLY_ONCE_xyz",
    );
    expect(rec.createTenantToken).toHaveLength(1);
    expect(rec.createTenantToken[0]?.body).toMatchObject({
      name: "ci-bot",
      scopes: ["skill:invoke"],
    });

    await page.getByTestId("token-plaintext-close-btn").click();
    await expect(plaintextDialog).toBeHidden();
  });

  test("revoke tenant token requires confirm and posts DELETE", async ({
    page,
  }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: ["token:read", "token:revoke"],
    });
    await mockAdmin(page, {
      tokens: {
        items: [
          {
            id: 100,
            tenant_id: 1,
            user_id: 42,
            workspace_id: 7,
            name: "ci-bot",
            prefix: "dft_abc12345",
            scopes: ["skill:invoke"],
            expires_at: null,
            last_used_at: null,
            revoked_at: null,
            created_at: "2026-04-01T00:00:00Z",
          },
        ],
        total: 1,
      },
    });
    const rec = await mockWrites(page);

    await page.goto("/admin/tokens");
    await page.getByTestId("token-revoke-100").click();
    await page.getByTestId("token-revoke-confirm-100").click();

    await expect.poll(() => rec.revokeTenantToken.length).toBe(1);
    expect(rec.revokeTenantToken[0]?.url).toContain("/api/tenants/1/tokens/100");
  });

  test("workspace add-member posts user_id and role", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: [
        "membership:read",
        "membership:invite",
        "membership:remove",
      ],
    });
    await mockAdmin(page, {
      workspaceMembers: {
        7: { items: [], total: 0 },
      },
    });
    const rec = await mockWrites(page);

    await page.goto("/admin/workspaces/7/members");
    await page.getByTestId("member-add-btn").click();
    await page.getByTestId("member-add-user-id").fill("11");
    await page.getByTestId("member-add-submit").click();

    await expect(page.getByTestId("member-add-dialog")).toBeHidden();
    expect(rec.addMember).toHaveLength(1);
    expect(rec.addMember[0]?.body).toEqual({ user_id: 11, role: "member" });
  });

  test("workspace remove-member posts DELETE on confirm", async ({ page }) => {
    await mockIdentity(page, {
      authenticated: true,
      permissions: [
        "membership:read",
        "membership:invite",
        "membership:remove",
      ],
    });
    await mockAdmin(page, {
      workspaceMembers: {
        7: {
          items: [
            {
              id: 11,
              email: "b@b.com",
              display_name: "Bob",
              avatar_url: null,
              status: 1,
              role: "member",
              joined_at: "2026-04-01T00:00:00Z",
            },
          ],
          total: 1,
        },
      },
    });
    const rec = await mockWrites(page);

    await page.goto("/admin/workspaces/7/members");
    await page.getByTestId("member-remove-11").click();
    await page.getByTestId("member-remove-confirm-11").click();

    await expect.poll(() => rec.removeMember.length).toBe(1);
    expect(rec.removeMember[0]?.url).toContain(
      "/api/tenants/1/workspaces/7/members/11",
    );
  });
});
