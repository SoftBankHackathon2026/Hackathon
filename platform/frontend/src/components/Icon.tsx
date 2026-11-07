export type IconName =
  | "grid"
  | "rocket"
  | "cloud"
  | "book"
  | "code"
  | "arrow"
  | "layers"
  | "pulse"
  | "users"
  | "shield"
  | "swap"
  | "unlink"
  | "coin"
  | "bot";
const paths: Record<IconName, string> = {
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  rocket:
    "M12 15l-3-3C10 6 15 3 21 3c0 6-3 11-9 12z M9 8H5l-3 5h6 M16 15v4l-5 3v-6 M5 17l-2 4 4-2 M15 7l2 2",
  cloud: "M7 18a5 5 0 1 1 .8-9.9 7 7 0 0 1 13.1 3A3.5 3.5 0 0 1 19 18H7z",
  book: "M12 5v16 M12 5C8 2 4 3 2 4v15c3-1 6-1 10 2 4-3 7-3 10-2V4c-3-1-7-2-10 1z",
  code: "M8 6l-6 6 6 6 M16 6l6 6-6 6 M14 3l-4 18",
  arrow: "M5 12h14 M13 6l6 6-6 6",
  layers: "M12 3l10 5-10 5L2 8z M2 12l10 5 10-5 M2 16l10 5 10-5",
  pulse: "M2 12h4l3-8 4 16 3-8h6",
  users:
    "M16 20v-1.5a3.5 3.5 0 0 0-3.5-3.5h-5A3.5 3.5 0 0 0 4 18.5V20 M10 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z M20 20v-1.5a3.5 3.5 0 0 0-2.6-3.4 M15.5 4.2a3.5 3.5 0 0 1 0 6.6",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z M9 12l2 2 4-4",
  swap: "M4 8h13 M13 4l4 4-4 4 M20 16H7 M11 12l-4 4 4 4",
  bot: "M12 3v3 M7 6h10a3 3 0 0 1 3 3v7a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V9a3 3 0 0 1 3-3z M9 11v2 M15 11v2 M9.5 16h5",
  unlink:
    "M10 13l-2 2a3 3 0 0 1-4-4l3-3a3 3 0 0 1 4 0 M14 11l2-2a3 3 0 0 1 4 4l-3 3a3 3 0 0 1-4 0 M3 3l2 2 M10 3v2 M3 8h2 M21 21l-2-2 M14 21v-2 M21 16h-2",
  coin: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z M15 9.5c0-1.4-1.3-2.5-3-2.5s-3 1-3 2.3c0 3 6 1.7 6 4.7 0 1.3-1.3 2.5-3 2.5s-3-1.1-3-2.5 M12 5.5V7 M12 17v1.5",
};
export function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d={paths[name]} />
    </svg>
  );
}
