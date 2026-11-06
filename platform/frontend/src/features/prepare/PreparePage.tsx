import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { PageHeader, Section } from "../../components/PageKit";
import { PageActions } from "../../components/PageSlots";
import { ProviderLogo } from "../../components/ProviderLogo";
import { fleetQueryOptions } from "../migrations/queries";
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

const STEPS = [
  {
    id: "connect",
    title: "환경 연결",
    detail: "출발/도착 환경을 플랫폼에 등록",
  },
  { id: "apps", title: "앱 선택", detail: "옮길 앱과 데이터 고르기" },
  {
    id: "precheck",
    title: "사전 점검",
    detail: "용량/권한/보안 정책 확인 후 이전 시작",
  },
];

const ROLE_LABELS = { legacy: "출발", target: "도착" } as const;

/** 설정 단계 하나: 번호(끝나면 체크) + 제목 + 설명 + 내용. */
function SetupTask({
  n,
  title,
  description,
  done,
  children,
}: {
  n: number;
  title: string;
  description?: string;
  done?: boolean;
  children: React.ReactNode;
}) {
  return (
    <li className={`setup-task${done ? " done" : ""}`}>
      <span className="setup-no" aria-hidden="true">
        {done ? (
          <svg viewBox="0 0 16 16">
            <path d="M3.5 8.5l3 3 6-7" />
          </svg>
        ) : (
          n
        )}
      </span>
      <div className="setup-task-body">
        <h3>{title}</h3>
        {description && <p className="setup-desc">{description}</p>}
        <div className="setup-content">{children}</div>
      </div>
    </li>
  );
}

/**
 * 새 이전 준비(Datadog 연동 설정 방식).
 * 왼쪽: 전체 단계와 이미 연결된 환경. 오른쪽: 번호 붙은 설정 단계. 아래: 진행 상태와 다음 버튼.
 */
export function PreparePage({
  onNavigate,
}: {
  onNavigate: (hash: string) => void;
}) {
  const fleet = useQuery(fleetQueryOptions()).data;
  const [kind, setKind] = useState<ShellKind>("kubernetes");
  const [k8s, setK8s] = useState<KubernetesInput>({
    name: "onprem-seoul",
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
  const formReady =
    kind === "kubernetes" ? kubernetesReady(k8s) : Boolean(aws.name.trim());
  const ran = kind === "kubernetes" ? checked : Boolean(ticket);
  const connected = fleet?.environments.filter((e) => e.connected) ?? [];

  function choose(next: ShellKind) {
    setKind(next);
    setTicket(null);
    setResult(null);
    setChecked(false);
  }

  return (
    <div className="pk-page">
      <PageActions>
        <span className="refresh refresh-idle">
          <i aria-hidden="true" />
          데모 화면
        </span>
      </PageActions>
      <PageHeader
        title="새 이전 준비"
        meta="환경을 연결하고 옮길 앱을 고르면, 사전 점검을 거쳐 이전 작업이 시작돼요."
      />
      <div className="setup">
        <aside className="setup-rail" aria-label="준비 단계">
          <ol className="setup-steps">
            {STEPS.map((step, i) => {
              const state = i === 0 ? (result ? "done" : "current") : "todo";
              return (
                <li
                  key={step.id}
                  className={`setup-step ${state}`}
                  aria-current={state === "current" ? "step" : undefined}
                >
                  <span className="setup-no" aria-hidden="true">
                    {state === "done" ? (
                      <svg viewBox="0 0 16 16">
                        <path d="M3.5 8.5l3 3 6-7" />
                      </svg>
                    ) : (
                      i + 1
                    )}
                  </span>
                  <span className="setup-step-text">
                    <b>{step.title}</b>
                    <small>{step.detail}</small>
                  </span>
                </li>
              );
            })}
          </ol>
          <section className="setup-connected">
            <h3>
              연결된 환경 <span>{connected.length}</span>
            </h3>
            {connected.length === 0 ? (
              <p className="pk-empty">아직 없어요.</p>
            ) : (
              <ul>
                {connected.map((env) => (
                  <li key={env.id}>
                    <ProviderLogo provider={env.provider} />
                    <span className="rule-text">
                      <span className="rule-title">{env.name}</span>
                      <span className="rule-sub">
                        {ROLE_LABELS[env.role]}, {env.region}
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </aside>

        <Section title="환경 연결" meta="1 / 3 단계">
          <ol className="setup-tasks">
            <SetupTask
              n={1}
              title="환경 종류"
              description="옮길 출발 환경이나 도착 환경을 고르세요."
              done
            >
              <div
                className="setup-kinds"
                role="radiogroup"
                aria-label="환경 종류"
              >
                {KIND_OPTIONS.map((option) => (
                  <button
                    key={option.value}
                    type="button"
                    role="radio"
                    aria-checked={kind === option.value}
                    disabled={option.disabled}
                    className="setup-kind"
                    onClick={() =>
                      !option.disabled && choose(option.value as ShellKind)
                    }
                  >
                    <ProviderLogo
                      provider={
                        option.value === "kubernetes" ? "onprem" : option.value
                      }
                    />
                    <span className="rule-text">
                      <span className="rule-title">
                        {option.title}
                        {option.disabled && (
                          <span className="setup-soon">준비 중</span>
                        )}
                      </span>
                      <span className="rule-sub">
                        {option.shell}, {option.detail}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            </SetupTask>

            <SetupTask
              n={2}
              title="연결 정보"
              description={
                kind === "kubernetes"
                  ? "kubectl 이 되는 접속지의 SSH 정보와 kubeconfig 만 주세요. 노드/CPU/메모리 같은 나머지는 플랫폼이 그 셸에서 읽어 와요."
                  : "이름과 리전, 읽기 권한 범위만 정해요. 계정 정보는 스크립트가 읽어 와요."
              }
              done={formReady}
            >
              {kind === "kubernetes" ? (
                <KubernetesForm value={k8s} onChange={setK8s} />
              ) : (
                <AwsForm value={aws} onChange={setAws} />
              )}
            </SetupTask>

            <SetupTask
              n={3}
              title={
                kind === "kubernetes" ? "연결 확인" : "셸에서 등록 명령 실행"
              }
              description={
                kind === "kubernetes"
                  ? "플랫폼이 접속지에 SSH 로 들어가 kubectl 로 클러스터를 읽어요."
                  : "aws 명령이 되는 셸에서 아래 명령을 차례로 실행하세요."
              }
              done={ran}
            >
              <div className="setup-run">
                {kind === "kubernetes" ? (
                  <button
                    type="button"
                    className="pk-button"
                    disabled={!formReady}
                    onClick={() => {
                      setChecked(true);
                      setResult(demoResult("kubernetes", k8s));
                    }}
                  >
                    {checked ? "다시 확인" : "연결 확인"}
                  </button>
                ) : (
                  <button
                    type="button"
                    className="pk-button"
                    disabled={!formReady}
                    onClick={() => {
                      setTicket(demoTicket());
                      setResult(null);
                    }}
                  >
                    {ticket ? "명령 다시 만들기" : "등록 명령 만들기"}
                  </button>
                )}
                <span className="setup-hint">
                  {!formReady
                    ? kind === "kubernetes"
                      ? "2단계의 접속지 주소/사용자/인증 정보/kubeconfig 를 채우면 확인할 수 있어요."
                      : "2단계의 환경 이름을 채우면 만들 수 있어요."
                    : kind === "aws" && ticket
                      ? `${TOKEN_TTL_MINUTES}분 동안 한 번만 쓸 수 있어요. 새로 만들면 이전 명령은 쓸 수 없어요.`
                      : ""}
                </span>
              </div>
              {kind === "kubernetes" && checked && (
                <ul className="setup-checks">
                  {connectionChecks(k8s).map((check) => (
                    <li key={check.label}>
                      <span className="setup-check-mark" aria-hidden="true">
                        <svg viewBox="0 0 16 16">
                          <path d="M3.5 8.5l3 3 6-7" />
                        </svg>
                      </span>
                      <span className="rule-text">
                        <span className="rule-title">{check.label}</span>
                        <span className="rule-sub">{check.detail}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {kind === "aws" && ticket && (
                <>
                  <Commands kind={kind} input={input} ticket={ticket} />
                  <div className="setup-does">
                    <h4>이 스크립트가 하는 일</h4>
                    <ul>
                      {SCRIPT_ACTIONS[kind].map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </div>
                </>
              )}
            </SetupTask>

            <SetupTask
              n={4}
              title="가져온 정보"
              description={
                kind === "kubernetes"
                  ? "접속지 셸에서 kubectl 로 읽은 클러스터 정보예요."
                  : "스크립트가 등록을 마치면 이 계정의 정보가 여기에 나와요."
              }
              done={Boolean(result)}
            >
              {result ? (
                <ResultView result={result} name={input.name} />
              ) : (
                <div className="setup-waiting">
                  <StatusDot tone="idle">
                    {kind === "aws" && ticket
                      ? `명령 실행을 기다리고 있어요, 등록 ID ${ticket.id}`
                      : "3단계를 마치면 채워져요."}
                  </StatusDot>
                  {kind === "aws" && ticket && (
                    <button
                      type="button"
                      className="pk-action"
                      onClick={() => setResult(demoResult(kind, input))}
                    >
                      데모 결과로 채우기
                    </button>
                  )}
                </div>
              )}
            </SetupTask>
          </ol>
        </Section>
      </div>

      <div className="setup-bar">
        <span className="setup-bar-status">
          {result ? (
            <StatusDot tone="ok">{input.name} 연결 완료</StatusDot>
          ) : (
            <StatusDot tone="idle">
              환경을 하나 이상 연결하면 다음 단계로 갈 수 있어요.
            </StatusDot>
          )}
          {result && <span className="sub">앱 선택 단계는 준비 중이에요.</span>}
        </span>
        <button
          type="button"
          className="pk-button weak"
          onClick={() => onNavigate("#migrations")}
        >
          취소
        </button>
        <button type="button" className="pk-button" disabled={!result}>
          다음: 앱 선택
        </button>
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
            placeholder="onprem-seoul"
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

      <section className="setup-sub">
        <h4>접속지 SSH</h4>
        <p className="setup-sub-help">
          kubectl 이 되는 호스트(배스천, 컨트롤 플레인 노드 등)예요.
        </p>
        <div className="prep-form prep-form-ssh">
          <Field label="IP 또는 호스트">
            <input
              value={value.host}
              onChange={(e) => set({ host: e.target.value })}
              placeholder="192.0.2.10"
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
      </section>

      <section className="setup-sub">
        <h4>Kubernetes 접속</h4>
        <p className="setup-sub-help">
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
      </section>
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
          className="pk-button weak prep-copy"
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
