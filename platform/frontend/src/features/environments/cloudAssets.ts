import { iconKey, type CloudIconRef } from "./cloudIcons";

const urls = import.meta.glob("../../assets/cloud-icons/*/*.svg", {
  eager: true,
  query: "?url",
  import: "default",
}) as Record<string, string>;

const raws = import.meta.glob("../../assets/cloud-icons/*/*.svg", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

function keyOf(path: string) {
  const [, provider, file] =
    /cloud-icons\/([^/]+)\/([^/]+)\.svg$/.exec(path) ?? [];
  return `${provider}/${file}`;
}

const byKey = new Map(Object.entries(urls).map(([p, u]) => [keyOf(p), u]));
const markupByKey = new Map(
  Object.entries(raws).map(([p, raw]) => {
    const start = raw.indexOf(">", raw.indexOf("<svg")) + 1;
    const inner = raw
      .slice(start, raw.lastIndexOf("</svg>"))
      .replace(/<title>[\s\S]*?<\/title>/g, "");
    return [keyOf(p), inner];
  }),
);

export function cloudIconUrl(ref: CloudIconRef | null) {
  return ref ? byKey.get(iconKey(ref)) : undefined;
}

export function cloudIconMarkup(ref: CloudIconRef | null) {
  return ref ? markupByKey.get(iconKey(ref)) : undefined;
}
