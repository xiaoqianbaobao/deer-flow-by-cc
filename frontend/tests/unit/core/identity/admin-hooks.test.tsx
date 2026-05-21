// frontend/tests/unit/core/identity/admin-hooks.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useSwitchTenant, useTenants } from "@/core/identity/hooks";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

describe("useTenants", () => {
  afterEach(() => vi.restoreAllMocks());

  it("returns OffsetListResponse<TenantRow>", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              id: 1,
              slug: "acme",
              name: "Acme",
              plan: "pro",
              status: 1,
              created_at: "2026-04-01T00:00:00+00:00",
            },
          ],
          total: 1,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useTenants({}), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.data?.items[0]?.slug).toBe("acme");
    expect(result.current.data?.total).toBe(1);
  });
});

describe("useSwitchTenant", () => {
  afterEach(() => vi.restoreAllMocks());

  it("POSTs /api/me/switch-tenant with the tenant_id", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ access_token: "t", expires_in: 900 }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    const { result } = renderHook(() => useSwitchTenant(), {
      wrapper: makeWrapper(),
    });
    await result.current.mutateAsync(7);
    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/me/switch-tenant",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({ tenant_id: 7 }),
      }),
    );
  });
});
