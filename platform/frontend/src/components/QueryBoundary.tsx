import { QueryErrorResetBoundary } from "@tanstack/react-query";
import { Suspense, type ReactNode } from "react";
import { ErrorBoundary, type FallbackProps } from "react-error-boundary";
import { errorMessage } from "../api/client";

export function LoadingNotice({ message }: { message?: string }) {
  return (
    <p className="empty" role="status">
      {message ?? "불러오는 중이에요…"}
    </p>
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
