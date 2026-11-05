import type { EnvComponent } from "../../api/migrations.ts";
import { BRAND_ICONS } from "./brandIcons.ts";
import { GLYPH_PATHS, glyphFor } from "./glyphs.ts";

export type IconSpec = { path: string; filled: boolean; slug: string };

export function brandSlug(
  component: Pick<EnvComponent, "id" | "kind">,
): string | null {
  const id = component.id.toLowerCase();
  if (id.includes("cloudflared")) return "cloudflare";
  if (id.includes("traefik")) return "traefikproxy";
  if (id === "argocd" || id.startsWith("argo-")) return "argo";
  if (id.includes("prometheus")) return "prometheus";
  switch (component.kind) {
    case "postgres":
      return "postgresql";
    case "kafka":
      return "apachekafka";
    case "vector_db":
      return "qdrant";
    case "network":
      return id.includes("tailscale") ? "tailscale" : null;
    default:
      return null;
  }
}

export function iconFor(
  component: Pick<EnvComponent, "id" | "kind">,
): IconSpec {
  const slug = brandSlug(component);
  if (slug && BRAND_ICONS[slug])
    return { path: BRAND_ICONS[slug], filled: true, slug };
  const glyph = glyphFor(component);
  return { path: GLYPH_PATHS[glyph], filled: false, slug: glyph };
}
