"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useEffect, useState } from "react";

export function AppProviders({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { retry: false, staleTime: 30_000 } },
      }),
  );
  useEffect(() => {
    // Expose the QueryClient on globalThis so E2E tests can invalidate identity
    // queries on demand (e.g. to trigger the session-expired modal without
    // waiting for staleTime). Minimal surface area — a single readonly handle.
    (globalThis as unknown as { __DEERFLOW_QC__?: QueryClient }).__DEERFLOW_QC__ = client;
  }, [client]);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
