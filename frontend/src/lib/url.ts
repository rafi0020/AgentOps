"use client";

import { usePathname, useRouter, useSearchParams } from "next/navigation";

/** Read and change query parameters; filters live in the URL so a view can be shared and survives a reload. */
export function useQueryState() {
  const params = useSearchParams();
  const router = useRouter();
  const path = usePathname();
  const set = (changes: Record<string, string | null | undefined>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(changes)) {
      if (v === null || v === undefined || v === "") next.delete(k);
      else next.set(k, v);
    }
    router.replace(`${path}?${next}`);
  };
  return { params, set, get: (k: string) => params.get(k) };
}
