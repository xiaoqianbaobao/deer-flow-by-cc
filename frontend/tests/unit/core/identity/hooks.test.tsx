// frontend/tests/unit/core/identity/hooks.test.tsx
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  useHasPermission,
  useIdentity,
  useLogout,
} from "@/core/identity/hooks";
import { type MeResponse } from "@/core/identity/types";

function makeWrapper() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return Wrapper;
}

const fakeMe: MeResponse = {
  user_id: 42,
  email: "demo@deerflow.local",
  display_name: "Demo",
  avatar_url: null,
  active_tenant_id: 1,
  tenants: [{ id: 1, slug: "default", name: "Default" }],
  workspaces: [{ id: 7, slug: "main", name: "Main" }],
  permissions: ["tenant:read", "workspace:read"],
  roles: { "tenant:1": ["tenant_owner"] },
};

describe("useIdentity", () => {
  afterEach(() => vi.restoreAllMocks());

  it("returns authenticated identity when /api/me resolves", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(fakeMe), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );

    const { result } = renderHook(() => useIdentity(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.identity).toEqual(fakeMe);
    expect(result.current.isAuthenticated).toBe(true);
  });

  it("reports unauthenticated when /api/me returns 401", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("", { status: 401 }),
    );

    const { result } = renderHook(() => useIdentity(), {
      wrapper: makeWrapper(),
    });

    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.isAuthenticated).toBe(false);
    expect(result.current.identity).toBeUndefined();
  });
});

describe("useHasPermission", () => {
  it("returns true when permission present", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(fakeMe), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useHasPermission("tenant:read"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current).toBe(true));
  });

  it("returns false when permission absent", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(fakeMe), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useHasPermission("audit:read"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current).toBe(false));
  });
});

describe("useLogout", () => {
  it("calls /api/auth/logout and invalidates identity query", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        new Response(JSON.stringify(fakeMe), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ status: "ok" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );

    const { result } = renderHook(() => useLogout(), {
      wrapper: makeWrapper(),
    });

    await result.current.mutateAsync();

    expect(fetchSpy).toHaveBeenCalledWith(
      "/api/auth/logout",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });
});
