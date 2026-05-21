// frontend/src/app/(public)/logout/page.tsx
"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useLogout } from "@/core/identity/hooks";

export default function LogoutPage() {
  const router = useRouter();
  const { mutateAsync } = useLogout();

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        await mutateAsync();
      } catch {
        // logout is best-effort
      } finally {
        if (!cancelled) router.replace("/login");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [mutateAsync, router]);

  return (
    <main className="flex min-h-screen items-center justify-center">
      <p className="text-muted-foreground">Signing you out…</p>
    </main>
  );
}
