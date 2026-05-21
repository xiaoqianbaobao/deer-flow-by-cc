// frontend/src/core/identity/components/PermBadge.tsx
import { Badge } from "@/components/ui/badge";

export function PermBadge({ perm }: { perm: string }) {
  return (
    <Badge variant="outline" className="font-mono text-xs">
      {perm}
    </Badge>
  );
}
