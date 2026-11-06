import { cloudIconUrl } from "../features/environments/cloudAssets";

/** 환경 표식. 클라우드마다 둥근 타일에 담아 표·경로에서 한눈에 구분되게 한다. */
export function ProviderLogo({ provider }: { provider: string | undefined }) {
  const url =
    provider === "aws"
      ? cloudIconUrl({ provider: "aws", file: "group-cloud" })
      : undefined;
  return (
    <span
      className={`pk-logo-tile tile-${provider ?? "unknown"}`}
      aria-hidden="true"
    >
      {url ? (
        <img className="pk-logo" src={url} alt="" />
      ) : (
        <svg
          className="pk-logo"
          width="16"
          height="16"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.6"
        >
          <rect x="2.5" y="2.5" width="11" height="4" rx="1" />
          <rect x="2.5" y="9.5" width="11" height="4" rx="1" />
          <path d="M5 4.5h.01M5 11.5h.01" strokeLinecap="round" />
        </svg>
      )}
    </span>
  );
}
