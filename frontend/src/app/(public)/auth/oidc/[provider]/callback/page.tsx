// frontend/src/app/(public)/auth/oidc/[provider]/callback/page.tsx
"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { use, useEffect } from "react";

interface Props {
  params: Promise<{ provider: string }>;
}

export default function OidcCallbackPage({ params }: Props) {
  const { provider } = use(params);
  const router = useRouter();
  const searchParams = useSearchParams();

  useEffect(() => {
    const qs = searchParams.toString();
    // Defer to backend which issues the cookie and redirects.
    window.location.replace(`/api/auth/oidc/${provider}/callback?${qs}`);
    // Fallback in case window.location is blocked in tests.
    const t = window.setTimeout(() => router.replace("/login"), 5000);
    return () => window.clearTimeout(t);
  }, [provider, searchParams, router]);

  return (
    <main className="flex min-h-screen items-center justify-center">
      <p className="text-muted-foreground">Completing sign-in…</p>
    </main>
  );
}
