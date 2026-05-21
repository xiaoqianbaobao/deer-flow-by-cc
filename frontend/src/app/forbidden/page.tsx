// frontend/src/app/forbidden/page.tsx
import Link from "next/link";

export default function ForbiddenPage() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-3">
      <h1 className="text-2xl font-semibold">403 — Permission required</h1>
      <p className="text-sm text-muted-foreground">
        You do not have permission to view this page.
      </p>
      <Link
        href="/admin/profile"
        className="text-sm underline hover:text-primary"
      >
        Go to my profile
      </Link>
    </main>
  );
}
