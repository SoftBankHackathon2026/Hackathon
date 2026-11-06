import { Icon } from "../../components/Icon";
import type { ConnectionTarget } from "./connectionTypes";
import { PROVIDERS, SCOPES } from "./model";

export function ConnectionTargetForm({
  value,
  onChange,
  onContinue,
}: {
  value: ConnectionTarget;
  onChange: (value: ConnectionTarget) => void;
  onContinue: () => void;
}) {
  const set = (patch: Partial<ConnectionTarget>) =>
    onChange({ ...value, ...patch });
  return (
    <form
      className="connection-target"
      onSubmit={(e) => {
        e.preventDefault();
        onContinue();
      }}
    >
      <header className="connection-title">
        <h2>어떤 환경을 연결할까요?</h2>
        <p>
          출발 환경이나 도착 환경을 선택하세요. 연결할 단말에서 명령 한 줄을
          실행하면 돼요.
        </p>
      </header>
      <fieldset className="connection-provider">
        <legend>환경 종류</legend>
        <div>
          {PROVIDERS.map((p) => (
            <label
              className={value.kind === p.value ? "selected" : ""}
              key={p.value}
            >
              <input
                type="radio"
                name="provider"
                value={p.value}
                checked={value.kind === p.value}
                onChange={() =>
                  set({
                    kind: p.value,
                    region:
                      p.value === "aws"
                        ? "ap-northeast-2"
                        : p.value === "gcp"
                          ? "asia-northeast3"
                          : "",
                    expectedIdentity: "",
                  })
                }
              />
              <Icon name={p.icon} size={22} />
              <span>
                <strong>{p.title}</strong>
                <small>{p.detail}</small>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="connection-fields">
        <label>
          조직
          <input
            required
            maxLength={80}
            value={value.organization}
            onChange={(e) => set({ organization: e.target.value })}
            placeholder="소속 조직"
            autoComplete="organization"
          />
        </label>
        <label>
          프로젝트
          <input
            required
            maxLength={80}
            value={value.project}
            onChange={(e) => set({ project: e.target.value })}
            placeholder="예: 주문 서비스 이관"
          />
        </label>
        <label>
          환경 이름
          <input
            required
            maxLength={80}
            value={value.name}
            onChange={(e) => set({ name: e.target.value })}
            placeholder={
              value.kind === "kubernetes"
                ? "예: busan-production"
                : value.kind === "aws"
                  ? "예: aws-seoul"
                  : "예: gcp-seoul"
            }
          />
        </label>
        <label>
          환경 역할
          <select
            value={value.role}
            onChange={(e) =>
              set({ role: e.target.value as ConnectionTarget["role"] })
            }
          >
            <option value="source">출발 환경</option>
            <option value="target">도착 환경</option>
          </select>
        </label>
        {value.kind !== "kubernetes" && (
          <>
            <label>
              리전
              <input
                required
                maxLength={64}
                value={value.region}
                onChange={(e) => set({ region: e.target.value })}
              />
            </label>
            <label>
              예상 {value.kind === "aws" ? "계정 ID" : "프로젝트 ID"}{" "}
              <span className="connection-optional">(선택)</span>
              <input
                maxLength={128}
                value={value.expectedIdentity}
                onChange={(e) => set({ expectedIdentity: e.target.value })}
                placeholder={
                  value.kind === "aws" ? "12자리 계정 번호" : "프로젝트 식별자"
                }
              />
              <small>터널 연결 후 실제 대상과 대조해요.</small>
            </label>
          </>
        )}
      </div>
      <fieldset className="connection-scopes">
        <legend>요청할 접근 범위</legend>
        <p>
          실제 자원과 권한은 연결 후 계획에서 검토해요. 비밀 원문은 화면에
          표시하지 않아요.
        </p>
        {SCOPES.map((scope) => (
          <label key={scope.value}>
            <input
              type="checkbox"
              checked={value.scopes.includes(scope.value)}
              disabled={scope.value === "inventory.read"}
              onChange={(e) =>
                set({
                  scopes: e.target.checked
                    ? [...value.scopes, scope.value]
                    : value.scopes.filter((s) => s !== scope.value),
                })
              }
            />
            <span>
              {scope.label}
              {scope.value === "inventory.read" && <small>필수</small>}
              <small>{scope.detail}</small>
            </span>
          </label>
        ))}
        <p className="connection-note">
          이관을 위한 생성·수정 권한은 별도 계획으로 승인해요.
        </p>
      </fieldset>
      <footer className="connection-actions">
        <button type="submit" className="primary">
          실행 준비 확인 <Icon name="arrow" size={16} />
        </button>
        <span>아직 권한이나 설치 내용은 변경하지 않아요.</span>
      </footer>
    </form>
  );
}
