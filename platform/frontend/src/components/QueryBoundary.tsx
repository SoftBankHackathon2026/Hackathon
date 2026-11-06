import { QueryErrorResetBoundary } from "@tanstack/react-query";
import { Suspense, type ReactNode } from "react";
import { ErrorBoundary, type FallbackProps } from "react-error-boundary";
import { errorMessage } from "../api/client";

/** 로딩은 문장 대신 자리 표시(스켈레톤). message 는 오래 걸리는 이유를 알려야 할 때만 쓴다. */
export function LoadingNotice({ message }: { message?: string }) {
  if (message)
    return (
      <p className="empty" role="status">
        {message}
      </p>
    );
  return <Skeleton />;
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="skeleton" role="status" aria-label="불러오는 중">
      {Array.from({ length: rows }, (_, i) => (
        <span key={i} />
      ))}
    </div>
  );
}

export function ErrorNotice({ error, resetErrorBoundary }: FallbackProps) {
  return (
    <div className="empty" role="alert">
      <p className="banner">{errorMessage(error, "불러오지 못했어요.")}</p>
      <button className="secondary" onClick={resetErrorBoundary}>
        다시 시도
      </button>
    </div>
  );
}

export function QueryErrorBoundary({
  children,
  resetKeys,
  fallback = ErrorNotice,
}: {
  children: ReactNode;
  resetKeys?: unknown[];
  fallback?: React.ComponentType<FallbackProps>;
}) {
  return (
    <QueryErrorResetBoundary>
      {({ reset }) => (
        <ErrorBoundary
          onReset={reset}
          resetKeys={resetKeys}
          FallbackComponent={fallback}
        >
          {children}
        </ErrorBoundary>
      )}
    </QueryErrorResetBoundary>
  );
}

export function PageBoundary({
  children,
  resetKeys,
  loading,
}: {
  children: ReactNode;
  resetKeys?: unknown[];
  loading?: ReactNode;
}) {
  return (
    <QueryErrorBoundary resetKeys={resetKeys}>
      <Suspense fallback={loading ?? <LoadingNotice />}>{children}</Suspense>
    </QueryErrorBoundary>
  );
}
