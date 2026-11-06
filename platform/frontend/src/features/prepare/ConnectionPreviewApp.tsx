import { useEffect, useState } from "react";
import { AppShell } from "../../components/AppShell";
import { useRoute } from "../../app/useRoute";
import { PreparePage } from "./PreparePage";
import {
  createPreviewGateway,
  PREVIEW_CODE,
  PREVIEW_SCENARIOS,
  type PreviewScenario,
} from "./previewGateway";
import type { CollectionContext, ConnectionSnapshot } from "./connectionTypes";

export function ConnectionPreviewApp() {
  const [gateway] = useState(() => createPreviewGateway(sessionStorage));
  const { route, go } = useRoute();
  useEffect(() => {
    document.title = "환경 연결 미리보기 · Oneport";
  }, []);
  return (
    <AppShell
      view="migrations"
      title="마이그레이션 준비"
      hideTitle
      account={
        <span className="connection-preview-account">로컬 미리보기</span>
      }
      onNavigate={(v) => {
        window.location.href = "/#" + v;
      }}
    >
      <PreparePage
        gateway={gateway}
        team="해커톤 팀"
        requestId={route.view === "prepare" ? route.requestId : undefined}
        onNavigate={(hash) => {
          if (hash === "#migrations") window.location.href = "/#migrations";
          else go(hash);
        }}
        previewTools={(snapshot, target) => (
          <PreviewControls
            key={snapshot?.id ?? "new"}
            gateway={gateway}
            snapshot={snapshot}
            target={target}
            onCreated={(id) => go("#prepare/" + id)}
          />
        )}
      />
    </AppShell>
  );
}

function PreviewControls({
  gateway,
  snapshot: s,
  target,
  onCreated,
}: {
  gateway: ReturnType<typeof createPreviewGateway>;
  snapshot: ConnectionSnapshot | null;
  target: CollectionContext;
  onCreated: (id: string) => void;
}) {
  const [scenario, setScenario] = useState<PreviewScenario>("pending"),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const perform = async (action: () => Promise<unknown>) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : "예시를 불러오지 못했어요.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="connection-preview-tools" aria-label="개발용 상태 재현">
      <h3>미리보기 조작</h3>
      <p>
        실제 단말·터널·고객 자원을 사용하지 않아요. 명령 복사 후 아래 버튼으로
        다음 상태를 재현하세요.
      </p>
      {s?.device === "PENDING_APPROVAL" && (
        <p>
          터미널 확인 코드 예시:{" "}
          <code>
            {PREVIEW_CODE.slice(0, 4)} {PREVIEW_CODE.slice(4)}
          </code>
        </p>
      )}
      <div className="connection-actions">
        {s?.registration === "ISSUED" && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() =>
              void perform(() => gateway.simulate(s.id, "execute"))
            }
          >
            단말 실행 재현
          </button>
        )}
        {s?.tunnel === "CONNECTING" && (
          <button
            className="secondary"
            disabled={busy}
            onClick={() => void perform(() => gateway.simulate(s.id, "tunnel"))}
          >
            터널 확인 재현
          </button>
        )}
      </div>
      <details>
        <summary>다른 상태 살펴보기</summary>
        <label>
          상태 예시
          <select
            value={scenario}
            onChange={(e) => setScenario(e.target.value as PreviewScenario)}
          >
            {PREVIEW_SCENARIOS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <div className="connection-actions">
          <button
            className="secondary"
            disabled={busy || (!s && !Boolean(target.organization.trim()))}
            onClick={() =>
              void perform(async () => {
                const request =
                  s ??
                  (await gateway.create(
                    target,
                    "preview-public-key",
                    crypto.randomUUID(),
                  ));
                await gateway.simulate(request.id, scenario);
                onCreated(request.id);
              })
            }
          >
            선택한 상태 불러오기
          </button>
        </div>
        {!s && !Boolean(target.organization.trim()) && (
          <p>조직을 먼저 확인하세요.</p>
        )}
      </details>
      {error && (
        <p className="connection-error" role="alert">
          {error}
        </p>
      )}
    </section>
  );
}
