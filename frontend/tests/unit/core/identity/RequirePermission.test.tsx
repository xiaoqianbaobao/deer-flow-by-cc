// frontend/tests/unit/core/identity/RequirePermission.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RequirePermission } from "@/core/identity/components/RequirePermission";
import { type MeResponse } from "@/core/identity/types";

function wrap(children: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

const me = (perms: string[]): MeResponse => ({
  user_id: 1,
  email: "a@b",
  display_name: null,
  avatar_url: null,
  active_tenant_id: 1,
  tenants: [],
  workspaces: [],
  permissions: perms,
  roles: {},
});

describe("<RequirePermission>", () => {
  afterEach(() => vi.restoreAllMocks());

  it("renders children when permission present", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(me(["tenant:read"])), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    render(
      wrap(
        <RequirePermission perm="tenant:read">
          <div>inside</div>
        </RequirePermission>,
      ),
    );

    await waitFor(() => expect(screen.getByText("inside")).toBeDefined());
  });

  it("renders 403 fallback when permission absent", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(me([])), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    render(
      wrap(
        <RequirePermission perm="audit:read">
          <div>secret</div>
        </RequirePermission>,
      ),
    );

    await waitFor(() =>
      expect(screen.getByText(/permission required/i)).toBeDefined(),
    );
    expect(screen.queryByText("secret")).toBeNull();
  });

  it("renders loading placeholder while identity is loading", () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      () =>
        new Promise(() => {
          /* never resolves — leaves useQuery in loading state */
        }),
    );

    render(
      wrap(
        <RequirePermission perm="tenant:read">
          <div>inside</div>
        </RequirePermission>,
      ),
    );

    expect(screen.getByRole("status")).toBeDefined();
  });
});
