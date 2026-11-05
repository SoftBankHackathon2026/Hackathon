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
  | "swap";
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
