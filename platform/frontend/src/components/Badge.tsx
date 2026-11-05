export function SourceTag({ source }: { source: string | undefined }) {
  if (source !== "simulated") return null;
  return (
    <span
      className="source-tag"
      title="실제 환경에서 수집한 값이 아니라 시뮬레이션으로 만든 값이에요."
    >
      시뮬레이션 데이터
    </span>
  );
}
