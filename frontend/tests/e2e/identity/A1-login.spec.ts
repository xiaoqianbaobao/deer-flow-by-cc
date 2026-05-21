// frontend/tests/e2e/identity/A1-login.spec.ts
import { expect, test } from "@playwright/test";

import { mockIdentity } from "./fixtures/mock-backend";

test.describe("A1: login + middleware guard", () => {
  test("unauthenticated /admin/profile redirects to /login?next=…", async ({
    page,
  }) => {
    await mockIdentity(page, { authenticated: false });

    await page.goto("/admin/profile");

    await expect(page).toHaveURL(/\/login\?next=%2Fadmin%2Fprofile/);
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  });

  test("login page renders provider buttons from /api/auth/providers", async ({
    page,
  }) => {
    await mockIdentity(page, { authenticated: false });

    await page.goto("/login");

    await expect(page.getByRole("link", { name: /Continue with Okta/ }))
      .toBeVisible();
    await expect(
      page.getByRole("link", { name: /Continue with Keycloak/ }),
    ).toBeVisible();
  });

  test("login page shows error banner when ?error=oidc_callback_failed", async ({
    page,
  }) => {
    await mockIdentity(page, { authenticated: false });

    await page.goto("/login?error=oidc_callback_failed");

    await expect(
      page.getByRole("alert").filter({ hasText: /sign-in via oidc/i }),
    ).toBeVisible();
  });

  test("authenticated /admin/profile renders identity info", async ({
    page,
  }) => {
    await mockIdentity(page, { authenticated: true });

    await page.goto("/admin/profile");

    await expect(page.getByRole("heading", { name: "Demo" })).toBeVisible();
    await expect(page.getByText("demo@deerflow.local")).toBeVisible();
    await expect(page.getByText("Main")).toBeVisible();
    await expect(page.locator("code", { hasText: "tenant:read" })).toBeVisible();
  });
});
