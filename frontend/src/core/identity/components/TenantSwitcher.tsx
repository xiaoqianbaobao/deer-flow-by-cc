// frontend/src/core/identity/components/TenantSwitcher.tsx
"use client";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

import { useIdentity, useSwitchTenant } from "../hooks";

export function TenantSwitcher() {
  const { identity } = useIdentity();
  const switcher = useSwitchTenant();

  if (!identity) return null;
  if (identity.tenants.length < 2) {
    // Single-tenant user: show name but no switcher.
    const only = identity.tenants[0];
    return (
      <span className="text-sm text-muted-foreground">
        {only?.name ?? "(no tenant)"}
      </span>
    );
  }

  const active = identity.tenants.find(
    (t) => t.id === identity.active_tenant_id,
  );

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="inline-flex h-8 items-center gap-2 rounded-md border px-3 text-sm hover:bg-accent"
        aria-label="Switch tenant"
      >
        <span className="font-medium">{active?.name ?? "Select tenant"}</span>
        <span aria-hidden>▾</span>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {identity.tenants.map((t) => (
          <DropdownMenuItem
            key={t.id}
            disabled={switcher.isPending || t.id === identity.active_tenant_id}
            onClick={() => switcher.mutate(t.id)}
          >
            {t.name}{" "}
            <span className="ml-2 text-xs text-muted-foreground">
              /{t.slug}
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
