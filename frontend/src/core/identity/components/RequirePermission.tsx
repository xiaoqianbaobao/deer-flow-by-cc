// frontend/src/core/identity/components/RequirePermission.tsx
"use client";

import { type ReactNode } from "react";

import { useIdentity } from "../hooks";
import { type Permission } from "../types";

interface Props {
  perm: Permission;
  children: ReactNode;
  fallback?: ReactNode;
}

export function RequirePermission({ perm, children, fallback }: Props) {
  const { identity, isLoading } = useIdentity();

  if (isLoading) {
    return (
      <div
        role="status"
        aria-live="polite"
        className="flex h-full w-full items-center justify-center p-8 text-sm text-muted-foreground"
      >
        Loading…
      </div>
    );
  }

  if (!identity?.permissions.includes(perm)) {
    if (fallback) return <>{fallback}</>;
    return (
      <div className="flex h-full w-full flex-col items-center justify-center gap-2 p-8">
        <h2 className="text-lg font-semibold">Permission required</h2>
        <p className="text-sm text-muted-foreground">
          You need the <code>{perm}</code> permission to view this page.
        </p>
      </div>
    );
  }

  return <>{children}</>;
}
