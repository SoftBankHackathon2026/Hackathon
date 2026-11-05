import { cloudIconUrl } from "../features/environments/cloudAssets";

export function ProviderLogo({ provider }: { provider: string | undefined }) {
  const url =
    provider === "aws"
      ? cloudIconUrl({ provider: "aws", file: "group-cloud" })
      : undefined;
  if (url) return <img className="pk-logo" src={url} alt="" />;
  return (
    <svg
      className="pk-logo"
      width="16"
      height="16"
      viewBox="0 0 16 16"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
    >
      <rect x="2.5" y="2.5" width="11" height="4" rx="1" />
      <rect x="2.5" y="9.5" width="11" height="4" rx="1" />
      <path d="M5 4.5h.01M5 11.5h.01" strokeLinecap="round" />
    </svg>
  );
}
