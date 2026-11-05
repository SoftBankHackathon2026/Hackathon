import { useEffect, useState } from "react";
import { PageHeader, Section } from "../../components/PageKit";
import { PageActions } from "../../components/PageSlots";
import { Segmented } from "../../components/Segmented";
import { StatusDot } from "../../components/StatusDot";
import {
  AWS_REGIONS,
  KIND_OPTIONS,
  KUBECONFIG_OPTIONS,
  SSH_AUTH_OPTIONS,
  ROLE_OPTIONS,
  SCOPE_OPTIONS,
  SCRIPT_ACTIONS,
  SCRIPT_NAMES,
  TOKEN_TTL_MINUTES,
  commandSteps,
  connectionChecks,
  demoResult,
  demoTicket,
  kubernetesReady,
  kubernetesSummary,
  scriptUrl,
  sha256Hex,
  type AwsInput,
  type AwsResult,
  type CommandStep,
  type EnvKind,
  type KubernetesInput,
  type KubernetesResult,
  type Ticket,
} from "./model";

type ShellKind = Exclude<EnvKind, "gcp">;

export function PreparePage({
  onNavigate,
}: {
  onNavigate: (hash: string) => void;
}) {
  const [kind, setKind] = useState<ShellKind>("kubernetes");
  const [k8s, setK8s] = useState<KubernetesInput>({
    name: "onprem-busan",
    role: "source",
    host: "",
    port: "22",
    user: "",
    auth: "key",
    privateKey: "",
    password: "",
    kubeconfigSource: "path",
    kubeconfigPath: "~/.kube/config",
    kubeconfigText: "",
    context: "",
  });
  const [checked, setChecked] = useState(false);
  const [aws, setAws] = useState<AwsInput>({
    name: "aws-seoul",
    role: "target",
    region: "ap-northeast-2",
    scope: "viewonly",
    roleName: "oneport-readonly",
    boundary: "",
  });
  const [ticket, setTicket] = useState<Ticket | null>(null);
  const [result, setResult] = useState<KubernetesResult | AwsResult | null>(
    null,
  );
  const input = kind === "kubernetes" ? k8s : aws;

  function choose(next: ShellKind) {
    setKind(next);
    setTicket(null);
    setResult(null);
    setChecked(false);
  }

  return (
    <div className="pk-page">
      <PageActions>
        <span className="pill">데모 화면</span>
      </PageActions>
      <PageHeader
        title="마이그레이션"
        tabs={[
          { id: "overview", label: "개요" },
          { id: "prepare", label: "준비" },
        ]}
        active="prepare"
        onTab={(id) => id === "overview" && onNavigate("#migrations")}
      />
      <ol className="prep-steps" aria-label="준비 단계">
        <li aria-current="step">
          <span className="prep-step-no">1</span>환경 연결
        </li>
        <li className={result ? "" : "disabled"}>
          <span className="prep-step-no">2</span>앱 선택
        </li>
      </ol>

      <Section
        title="환경 종류"
        description="옮길 출발 환경이나 도착 환경을 고르세요. 클러스터 정보는 직접 입력하지 않고, 그 환경에 명령이 되는 셸에서 읽어 와요."
      >
        <div className="prep-kinds" role="radiogroup" aria-label="환경 종류">
          {KIND_OPTIONS.map((option) => (
            <button
              key={option.value}
              type="button"
              role="radio"
              aria-checked={kind === option.value}
              disabled={option.disabled}
              className="prep-kind"
              onClick={() =>
                !option.disabled && choose(option.value as ShellKind)
              }
            >
              <span className="prep-radio" aria-hidden="true" />
              <span className="prep-kind-text">
                <strong>{option.title}</strong>
                <small>
                  {option.shell} · {option.detail}
                </small>
              </span>
            </button>
          ))}
        </div>
      </Section>

      <Section
        title="플랫폼에 줄 정보"
        description={
          kind === "kubernetes"
            ? "kubectl 이 되는 접속지의 SSH 정보와 kubeconfig 를 주세요. 노드 수·CPU·메모리 같은 나머지는 플랫폼이 그 셸에서 읽어 와요."
            : "이름과 리전, 읽기 권한 범위만 정해요. 계정 정보는 스크립트가 읽어 와요."
        }
      >
        {kind === "kubernetes" ? (
          <KubernetesForm value={k8s} onChange={setK8s} />
        ) : (
          <AwsForm value={aws} onChange={setAws} />
        )}
        <div className="prep-actions">
          {kind === "kubernetes" ? (
            <button
              type="button"
              className="primary"
              disabled={!kubernetesReady(k8s)}
              onClick={() => {
                setChecked(true);
                setResult(demoResult("kubernetes", k8s));
              }}
            >
              {checked ? "다시 연결 확인" : "연결 확인"}
            </button>
          ) : (
            <button
              type="button"
              className="primary"
              disabled={!input.name.trim()}
              onClick={() => {
                setTicket(demoTicket());
                setResult(null);
              }}
            >
              {ticket ? "등록 명령 다시 만들기" : "등록 명령 만들기"}
            </button>
          )}
          {kind === "kubernetes" && !kubernetesReady(k8s) && (
            <span className="prep-note">
              접속지 주소, 사용자, 인증 정보, kubeconfig 를 모두 채우면 확인할
              수 있어요.
            </span>
          )}
          {kind === "aws" && ticket && (
            <span className="prep-note">
              {TOKEN_TTL_MINUTES}분 동안 한 번만 쓸 수 있어요. 새로 만들면 이전
              명령은 쓸 수 없어요.
            </span>
          )}
        </div>
      </Section>

      {kind === "kubernetes" && checked && (
        <Section
          title="연결 확인"
          description="플랫폼이 접속지에 SSH 로 들어가 kubectl 로 클러스터를 읽었어요. (데모)"
        >
          <ul className="prep-checks">
            {connectionChecks(k8s).map((check) => (
              <li key={check.label}>
                <StatusDot tone="ok">{check.label}</StatusDot>
                <span className="prep-check-detail">{check.detail}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {kind === "aws" && ticket && (
        <Section
          title="셸에서 실행"
          description="aws 명령이 되는 셸에서 아래 명령을 차례로 실행하세요."
        >
          <Commands kind={kind} input={input} ticket={ticket} />
          <div className="prep-does">
            <h3>이 스크립트가 하는 일</h3>
            <ul>
              {SCRIPT_ACTIONS[kind].map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </div>
        </Section>
      )}

      {((kind === "aws" && ticket) || (kind === "kubernetes" && checked)) && (
        <Section
          title="가져온 정보"
          description={
            kind === "kubernetes"
              ? "플랫폼이 접속지 셸에서 kubectl 로 읽은 클러스터 정보예요."
              : "스크립트가 등록을 마치면 이 계정의 정보가 여기에 나와요."
          }
          action={
            result || !ticket
              ? undefined
              : {
                  label: "데모 결과로 채우기",
                  onClick: () => setResult(demoResult(kind, input)),
                }
          }
        >
          {result ? (
            <ResultView result={result} name={input.name} />
          ) : (
            <p className="prep-waiting">
              <StatusDot tone="idle">
                명령 실행을 기다리고 있어요. 등록 ID {ticket?.id}
              </StatusDot>
            </p>
          )}
        </Section>
      )}

      <div className="prep-footer">
        <button type="button" className="primary" disabled={!result}>
          다음: 앱 선택
        </button>
        <span className="prep-note">
          {result
            ? "앱 선택 단계는 준비 중이에요."
            : "환경을 하나 이상 연결하면 다음 단계로 갈 수 있어요."}
        </span>
      </div>
    </div>
  );
}

function Field({
  label,
  help,
  children,
}: {
  label: string;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="prep-field">
      <span className="prep-label">{label}</span>
      {children}
      {help && <small className="prep-help">{help}</small>}
    </label>
  );
}

function KubernetesForm({
  value,
  onChange,
}: {
  value: KubernetesInput;
  onChange: (value: KubernetesInput) => void;
}) {
  const set = (patch: Partial<KubernetesInput>) =>
    onChange({ ...value, ...patch });
  return (
    <div className="prep-groups">
      <div className="prep-form">
        <Field label="환경 이름" help="화면과 이전 작업에 쓰는 이름이에요.">
          <input
            value={value.name}
            onChange={(e) => set({ name: e.target.value })}
            placeholder="onprem-busan"
          />
        </Field>
        <Field label="역할">
          <Segmented
            label="역할"
            value={value.role}
            options={ROLE_OPTIONS}
            onChange={(role) => set({ role })}
          />
        </Field>
      </div>

      <fieldset className="prep-group">
        <legend>접속지 SSH</legend>
        <p className="prep-help">
          kubectl 이 되는 호스트(배스천, 컨트롤 플레인 노드 등)예요.
        </p>
        <div className="prep-form prep-form-ssh">
          <Field label="IP 또는 호스트">
            <input
              value={value.host}
              onChange={(e) => set({ host: e.target.value })}
              placeholder="172.30.1.10"
            />
          </Field>
          <Field label="포트">
            <input
              value={value.port}
              inputMode="numeric"
              onChange={(e) => set({ port: e.target.value })}
              placeholder="22"
            />
          </Field>
          <Field label="사용자 ID">
            <input
              value={value.user}
              autoComplete="off"
              onChange={(e) => set({ user: e.target.value })}
              placeholder="ubuntu"
            />
          </Field>
          <Field label="인증 방식">
            <Segmented
              label="인증 방식"
              value={value.auth}
              options={SSH_AUTH_OPTIONS}
              onChange={(auth) => set({ auth })}
            />
          </Field>
        </div>
        {value.auth === "key" ? (
          <Field
            label="SSH 개인키"
            help="이 접속에만 쓰는 키를 권해요. 플랫폼은 키를 화면에 다시 보여 주지 않아요."
          >
            <textarea
              className="prep-secret"
              rows={4}
              spellCheck={false}
              value={value.privateKey}
              onChange={(e) => set({ privateKey: e.target.value })}
              placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
            />
          </Field>
        ) : (
          <Field label="비밀번호">
            <input
              type="password"
              autoComplete="new-password"
              value={value.password}
              onChange={(e) => set({ password: e.target.value })}
            />
          </Field>
        )}
      </fieldset>

      <fieldset className="prep-group">
        <legend>Kubernetes 접속</legend>
        <p className="prep-help">
          접속지 셸에서 kubectl 이 쓸 kubeconfig 예요.
        </p>
        <div className="prep-form">
          <Field label="kubeconfig">
            <Segmented
              label="kubeconfig"
              value={value.kubeconfigSource}
              options={KUBECONFIG_OPTIONS}
              onChange={(kubeconfigSource) => set({ kubeconfigSource })}
            />
          </Field>
          <Field
            label="context (선택)"
            help="비우면 kubeconfig 의 현재 context 를 써요."
          >
            <input
              value={value.context}
              onChange={(e) => set({ context: e.target.value })}
              placeholder="hackathon"
            />
          </Field>
        </div>
        {value.kubeconfigSource === "path" ? (
          <Field label="접속지의 kubeconfig 경로">
            <input
              value={value.kubeconfigPath}
              onChange={(e) => set({ kubeconfigPath: e.target.value })}
              placeholder="~/.kube/config"
            />
          </Field>
        ) : (
          <Field label="kubeconfig 내용">
            <textarea
              className="prep-secret"
              rows={5}
              spellCheck={false}
              value={value.kubeconfigText}
              onChange={(e) => set({ kubeconfigText: e.target.value })}
              placeholder="apiVersion: v1&#10;kind: Config&#10;..."
            />
          </Field>
        )}
      </fieldset>
    </div>
  );
}

function AwsForm({
  value,
  onChange,
}: {
  value: AwsInput;
  onChange: (value: AwsInput) => void;
}) {
  const scope = SCOPE_OPTIONS.find((s) => s.value === value.scope);
  return (
    <div className="prep-form">
      <Field label="환경 이름" help="화면과 이전 작업에 쓰는 이름이에요.">
        <input
          value={value.name}
          onChange={(e) => onChange({ ...value, name: e.target.value })}
          placeholder="aws-seoul"
        />
      </Field>
      <Field label="역할">
        <Segmented
          label="역할"
          value={value.role}
          options={ROLE_OPTIONS}
          onChange={(role) => onChange({ ...value, role })}
        />
      </Field>
      <Field label="리전">
        <select
          value={value.region}
          onChange={(e) => onChange({ ...value, region: e.target.value })}
        >
          {AWS_REGIONS.map((region) => (
            <option key={region} value={region}>
              {region}
            </option>
          ))}
        </select>
      </Field>
      <Field label="읽기 권한 범위" help={scope?.detail}>
        <Segmented
          label="읽기 권한 범위"
          value={value.scope}
          options={SCOPE_OPTIONS.map(({ value: v, label }) => ({
            value: v,
            label,
          }))}
          onChange={(next) => onChange({ ...value, scope: next })}
        />
      </Field>
      <Field
        label="역할 이름"
        help="계정 정책상 접두사가 정해져 있으면 바꿔요."
      >
        <input
          value={value.roleName}
          onChange={(e) => onChange({ ...value, roleName: e.target.value })}
          placeholder="oneport-readonly"
        />
      </Field>
      <Field
        label="권한 경계 정책 ARN (선택)"
        help="계정이 권한 경계를 요구할 때만 넣어요."
      >
        <input
          value={value.boundary}
          onChange={(e) => onChange({ ...value, boundary: e.target.value })}
          placeholder="arn:aws:iam::<계정>:policy/<경계 정책>"
        />
      </Field>
    </div>
  );
}

function Commands({
  kind,
  input,
  ticket,
}: {
  kind: ShellKind;
  input: KubernetesInput | AwsInput;
  ticket: Ticket;
}) {
  const origin = window.location.origin;
  const [sha, setSha] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setSha(null);
    fetch(scriptUrl(origin, kind))
      .then((r) =>
        r.ok ? r.text() : Promise.reject(new Error(String(r.status))),
      )
      .then(sha256Hex)
      .then((digest) => alive && setSha(digest))
      .catch(() => alive && setSha("스크립트를 불러오지 못했어요"));
    return () => {
      alive = false;
    };
  }, [origin, kind]);
  const steps = commandSteps(kind, input, ticket, origin, sha);
  return (
    <div className="prep-commands">
      {steps.map((step) => (
        <CommandBlock key={step.title} step={step} />
      ))}
      <p className="prep-note">
        스크립트 {SCRIPT_NAMES[kind]} 은 비밀값이 없는 공개 파일이에요. 토큰은
        명령에만 들어 있어요.
      </p>
    </div>
  );
}

function CommandBlock({ step }: { step: CommandStep }) {
  const [copied, setCopied] = useState(false);
  const text = step.lines.join("\n");
  return (
    <div className="prep-command">
      <div className="prep-command-head">
        <h3>{step.title}</h3>
        <button
          type="button"
          className="secondary prep-copy"
          onClick={() => {
            navigator.clipboard?.writeText(text).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            });
          }}
        >
          {copied ? "복사했어요" : "복사"}
        </button>
      </div>
      <pre className="prep-code">
        <span className="prep-comment">{step.comment}</span>
        {"\n"}
        {step.lines.map((line) => (
          <span key={line} className="prep-line">
            <span className="prep-prompt" aria-hidden="true">
              {line.startsWith(" ") ? "  " : "$ "}
            </span>
            {line}
            {"\n"}
          </span>
        ))}
      </pre>
    </div>
  );
}

function ResultView({
  result,
  name,
}: {
  result: KubernetesResult | AwsResult;
  name: string;
}) {
  if (result.kind === "aws") {
    return (
      <div className="prep-result">
        <p className="prep-headline">
          <StatusDot tone="ok">등록을 마쳤어요.</StatusDot> {name} 계정의{" "}
          {result.region}
          리전을 읽기 전용 역할로 볼 수 있어요.
        </p>
        <dl className="ux-kv prep-kv">
          <div>
            <dt>계정</dt>
            <dd>
              {result.account} ({result.alias})
            </dd>
          </div>
          <div>
            <dt>읽기 역할</dt>
            <dd>{result.roleArn}</dd>
          </div>
          {result.counts.map((c) => (
            <div key={c.label}>
              <dt>{c.label}</dt>
              <dd>{c.value}개</dd>
            </div>
          ))}
        </dl>
      </div>
    );
  }
  const summary = kubernetesSummary(result);
  return (
    <div className="prep-result">
      <p className="prep-headline">
        <StatusDot tone="ok">등록을 마쳤어요.</StatusDot> {summary.sentence}
      </p>
      <table className="pk-table">
        <thead>
          <tr>
            <th>노드</th>
            <th>역할</th>
            <th className="num">CPU</th>
            <th className="num">메모리</th>
            <th>OS</th>
            <th>런타임</th>
            <th>내부 IP</th>
          </tr>
        </thead>
        <tbody>
          {result.nodes.map((node) => (
            <tr key={node.name}>
              <td>{node.name}</td>
              <td>{node.role === "control" ? "컨트롤 플레인" : "워커"}</td>
              <td className="num">{node.cpu}코어</td>
              <td className="num">{node.memoryGiB}GiB</td>
              <td>{node.os}</td>
              <td>{node.runtime}</td>
              <td>{node.ip}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <dl className="ux-kv prep-kv">
        <div>
          <dt>버전</dt>
          <dd>
            {result.version} ({result.distribution})
          </dd>
        </div>
        <div>
          <dt>kube API</dt>
          <dd>{result.server}</dd>
        </div>
        <div>
          <dt>StorageClass</dt>
          <dd>{result.storageClasses.join(", ")}</dd>
        </div>
        <div>
          <dt>IngressClass</dt>
          <dd>{result.ingressClasses.join(", ")}</dd>
        </div>
        <div>
          <dt>네임스페이스</dt>
          <dd>{result.namespaces}개</dd>
        </div>
        <div>
          <dt>설치된 도구</dt>
          <dd>{result.controllers.join(", ")}</dd>
        </div>
      </dl>
    </div>
  );
}
